---
title: Target interface
kind: spec
id: S0002
status: implemented
superseded_by:
related: [D5, D6, D10, D16, D17, D18, D23, D24, S0001, S0003]
owner: maintainer
updated: 2026-11-05
---

# S0002 Target interface

## Summary
The runtime drives every target (`pyc` now, `jit-x86_64` in phase 3, `mpy` later) through one small
internal interface, so one test suite and one fallback rule serve all of them
([D5](../DECISIONS.md)). This fixes that interface for phase 3, the uniform trap type that replaces
`pyc`'s provisional export wrapper, the per-module fallback, and how the
[S0001](S0001-engine-interface.md) adapters sit on it. The JIT side of the boundary is
[S0003](S0003-jit-context-abi.md). Not the public API (phase 6).

## Goals / Non-goals
* Goals: what the runtime asks a target, with exact types; one `Trap`; host exceptions that cross
  any target unchanged; values that are lossless at the boundary; room for a second JIT architecture.
* Non-goals: `Engine`/`Store`/`Linker` of wasmtime-py; serialization and the cache (`compile` returns
  an in-memory object; `serialize` comes in phase 5–6); fuel and epochs; references, tables and
  memory sharing across targets (below, "later"); `mpy`.

## Interface
`src/waboros/targets/base.py` (stdlib only). Internal; pyright strict.

```python
class TrapCode(IntEnum):    # numbers and names of ARTIFACT.md part 3 (Wasmtime's names, D6)
    UNREACHABLE = 1; MEMORY_OUT_OF_BOUNDS = 2; INTEGER_DIVISION_BY_ZERO = 3; INTEGER_OVERFLOW = 4
    STACK_OVERFLOW = 5; BAD_CONVERSION_TO_INTEGER = 6; INDIRECT_CALL_TO_NULL = 7
    BAD_SIGNATURE = 8; TABLE_OUT_OF_BOUNDS = 9; NULL_REFERENCE = 10; INTERRUPT = 12
    UNCAUGHT_EXCEPTION = 14

class Trap(Exception):      # the only trap type of the package; targets raise nothing else for traps
    def __init__(self, code: TrapCode, message: str = "") -> None: ...
    code: TrapCode
    exn: tuple[object, tuple[object, ...]] | None   # UNCAUGHT_EXCEPTION only: (tag, payload)

class Unsupported(Exception):       # a valid module this target cannot compile (yet)
    def __init__(self, target: str, feature: str) -> None: ...

class HostError(Exception):         # carrier: a host function raised `exc`; never seen by callers
    exc: BaseException

Raw: TypeAlias = int        # a 64-bit slot: i32 in [0, 2**32), i64 in [0, 2**64),
                            # f32/f64 as bit patterns (ARTIFACT.md part 3 slot encoding)
RefVal: TypeAlias = object  # funcref/externref at the boundary: an opaque object, None = null
HostFunc: TypeAlias = Callable[[Sequence[Raw]], Sequence[Raw]]   # wrapped by the runtime

@dataclass(frozen=True)
class Extern:               # a resolved import, already type-checked by the runtime
    kind: Literal["func", "memory", "table", "global", "tag"]
    owner: str | None       # target name of the exporting instance; None for host objects
    value: object           # HostFunc | target-specific handle (opaque to other targets)

class Target(Protocol):
    name: str               # "pyc", "jit-x86_64"
    version: int            # bumps invalidate cached code
    def probe(self) -> str | None: ...                       # None: usable here; else why not
    def supports(self, module: ValidModule) -> str | None: ...  # None, or the first missing feature
    def compile(self, module: ValidModule) -> object: ...      # may raise Unsupported
    def instantiate(self, compiled: object, imports: Sequence[Extern],
                    store: StoreState) -> TargetInstance: ...  # segments, start; may raise Trap

class TargetInstance(Protocol):
    def call(self, func_index: int, args: Sequence[Raw]) -> list[Raw]: ...     # Trap / host exc
    def export(self, name: str) -> Extern: ...                 # LookupError if absent
    def global_get(self, global_index: int) -> Raw: ...
    def limits(self, name: str) -> tuple[int, int | None]: ...  # exported memory/table: (size, max)
    def global_set(self, global_index: int, value: Raw) -> None: ...           # later (phase 6)
    def interrupt(self) -> None: ...                           # JIT: sets the word; pyc: later
```
* `ValidModule` is what the front end's validator returns (decoded sections, function types,
  per-function bodies already validated). No target ever sees an invalid module. While the validator
  does not cover a construct (typed function references, tags, some table instructions), it ends
  without a verdict and marks the module `validated = False`: `pyc` may run it (a Python exception is
  safe), a JIT target's `supports()` refuses it, so machine code is only ever generated for fully
  validated modules.
* `StoreState` (internal, `src/waboros/runtime/store.py`): the owner thread, the entry depth, the
  pending host exception, `max_wasm_stack`, the stack bounds the JIT needs (S0003), the list of
  live instances for `interrupt`. The phase-6 `Store` wraps it.
* Registry: `TARGETS: dict[str, Callable[[], Target]]` in `base.py`; `select(module, order)` returns
  the first target whose `probe()` and `supports(module)` are `None`.

## Semantics
**Traps.** Every target reports a trap as `Trap(code)`; the conversion from target-specific signals
happens at **one place per target: where control enters its code** (`TargetInstance.call`,
`instantiate`, and the callable a target hands out for its exported functions). `pyc`: the
generated code raises `Trap` where it checks; Python errors that mean a trap (`IndexError`,
`struct.error` on memory, `ZeroDivisionError`, `RecursionError` → `STACK_OVERFLOW`) are converted
there. This replaces `_export` in `targets/pyc/runtime.py` and the string codes of `pyc`'s `Trap`.
JIT: the entry's status word → `Trap(TrapCode(status))` (S0003).

**Host exceptions.** The runtime wraps every host function it passes as an import so that any
exception it raises becomes `HostError(exc)`. A target lets `HostError` cross its code unchanged
(`pyc`: plain propagation, never converted to a trap — today an `IndexError` raised by a host
function would become `MEMORY_OUT_OF_BOUNDS`; JIT: stored in `StoreState`, unwound as status
`HOST_ERROR`, S0003). At the outermost entry the runtime re-raises `HostError.exc`, so callers see
the original exception (TARGETS.md; `KeyboardInterrupt` included).

**Values.** `Raw` everywhere at this boundary: lossless (f32 signalling NaNs survive, S0001
"Values"), language-neutral (D17), and the JIT's slot format as is. `pyc` converts at its entry.
References (`funcref`, `externref`): later, with stage 2 (TARGETS.md).

**Linking.** The runtime, not the target, resolves `(module, field)` to an `Extern` and checks its
type (today's code in `tests/spec/pyc_engine.py` moves to `src/waboros/runtime/link.py`). Functions
stay within one target (D25): every instance of a store runs on the store's target, so functions,
memories, tables and globals are shared only between instances of that target; host functions come
in as `HostFunc`.

**Target choice (D25).** The target is fixed per engine (and so per store): `auto` picks
`jit-<arch>` when `probe()` succeeds (once per process, cached), else `pyc`; the module's content
never changes the choice. `WABOROS_TARGET` overrides it. A module the chosen target cannot compile
raises `Unsupported` with the feature — there is no per-module fallback and no Python helper for a
computation the JIT has not implemented (D25); only runtime services (`memory.grow`, `table.grow`,
table/segment initialisation) are called through the host bridge. A module without a full validator
verdict (`validated = False`) is `Unsupported` on a JIT. `auto` selects `jit-<arch>` only when its
expected failures equal `pyc`'s in the claimed groups (until then a readiness guard keeps it off). A target never special-cases another.

**Threads and depth.** One thread per store: `StoreState` records the owner on first entry (the
first claim is atomic: of two threads entering a fresh store, one wins and the other gets
`RuntimeError`) and raises `RuntimeError` on entry from another thread, before it changes any of the
store's state (counters read before the check are written back only by the owner thread); nesting (wasm → host → wasm) on the owner thread is allowed. JIT
`Memory.grow`, `Table.grow` and instantiation also refuse a thread other than the owner; on a store
with no owner yet they hold a claim only while they run, so a store built on one thread can run on
another (S0009 step 2b). Depth is bounded by each target (D10): `pyc` as ARCHITECTURE.md describes; JIT S0003.

**Spec runner.** `tests/spec/pyc_engine.py` and a new `tests/spec/jit_engine.py` become thin S0001
adapters over `select`/`compile`/`instantiate`/`call`: signed ↔ `Raw` conversion, `Trap` →
S0001 `Trap(TrapCode[code.name])`, front-end errors → `LoadError`. `--engine jit` uses the order
`["jit-x86_64"]` with **no fallback**: a valid module the JIT does not support fails its commands
with `unsupported: <feature>`, listed in `xfail/jit.txt`, so the JIT's number is its own.
`--engine auto` follows the target choice above.

## Test plan
Phase-3 steps, each one commit with its acceptance; the reviewer reads every JIT/ABI diff (CLAUDE.md).

1. **This interface for `pyc`.** `base.py`, `runtime/link.py`, `StoreState`; `pyc` behind it; the
   adapter thinned. `uv run python -m tests.spec.run --engine pyc` → the same pass count as before,
   0 fail/xpass/stale; `uv run pytest tests/test_pyc.py` with a host function raising `IndexError`
   (must come out as `IndexError`) and `ValueError` across a nested call.
2. **Encoder + executable memory + probe.** Port `Asm` from `poc/w2jit.py` as the x86-64 encoder
   (labels and fixups only, LESSONS 6); W^X mapping (RW → RX via ctypes `mprotect`); probe.
   `uv run pytest tests/jit/test_encoder.py`: every form the encoder emits, with registers 0–15 and
   rsp/rbp/r12/r13 bases, equals `llvm-mc --show-encoding` output (dev tool; skipped if absent).
3. **The JIT runs wasm**, four sub-steps, each one commit and one review (engine names are target
   names: `--engine jit-x86_64`, never a bare `jit`; one adapter `tests/spec/target_engine.py` for
   every waboros target; the runner instantiates `spectest` lazily; `tools/spec_diff.py A B` lists
   commands passing on A and not on B):
   * **3a ABI**: context, value stack, SysV entry (Win64 shape), trap-by-return, host bridge with
     nested entries and host exceptions, stack limits, interrupt — tested on hand-built functions;
     every core command `unsupported` on `jit-x86_64`, run exits 0.
   * **3b one-pass core + integer backend**: i32/i64, locals, globals, control flow incl. `br_table`,
     block params, multi-value, direct calls, loads/stores with explicit bounds checks; floats as data
     only. `spec_diff pyc jit-x86_64` on the files it claims → 0 regressions.
     Done: core 5370 pass, 0 fail, the rest `unsupported`; `spec_diff pyc jit-x86_64` lists only
     `unsupported:` lines and linking.wast:564 (a cascade of the unsupported `memory.grow`, closes in 3c).
   * **3c native floats (SSE2 baseline) + `memory.size`/`memory.grow`** (grow is a runtime service:
     commit pages in the 4 GiB reservation, S0003). First CoreMark checkpoint.
   * **3d tables, funcref, `call_indirect`** (table services through the bridge). `spec_diff pyc
     jit-x86_64 core` → only `unsupported:` lines remain; second CoreMark checkpoint vs `w2jit2`.
4. **Fallback test only.** A forced probe failure selects `pyc` for the engine (D25).
   Done: `auto` warns once (`JitUnavailableWarning`, D31) whenever it falls back to `pyc` (the JIT for
   this machine's architecture cannot run, or there is no JIT for it); `tests/test_fallback.py` forbids executable memory for real (`PR_SET_MDWE` in a
   subprocess; skipped where the kernel lacks it) and CI runs the spec under `tools/no_exec.py`.
5. **Speed and ISA level.** CoreMark on the package JIT alternated with `poc/run_jit2.py` in one
   session, row in BENCHMARKS.md: target ≥ 95 % of `w2jit2` (trap-by-return and interrupt polls cost
   something; measured, not assumed). The ISA check: `objdump -D` of the code of every spec module
   and CoreMark, only code ranges (jump tables live in a data area, S0003), every mnemonic in the
   x86-64-v1 allowlist; it must fail on a planted `roundsd` and pass when v2 is declared.
   Done: CoreMark 17 526 vs `w2jit2` 17 662 it/s (medians of 5 alternated rounds, 99.2 %), see
   BENCHMARKS.md "JIT step 5 checkpoint". ISA: `tools/isa_all.py` (CI job `jit`) compiles every spec
   module with `WABOROS_JIT_DUMP` plus CoreMark and runs `tools/isa_check.py --data` on each non-empty
   blob (379 blobs, 0 failures); `isa_check.py --level v2` declares the SSE3–SSE4.2/popcnt level;
   `tests/jit/test_encoder.py::test_isa_levels` plants `roundsd`/`popcnt` (fail at v1, pass at v2)
   and AVX (fails at both).
6. **Reviewer pass** over steps 2–5 (fresh context), then S0002/S0003 → `implemented`.

**Ported vs rewritten.** Ported (with tests): the encoder; pin choice by loop-depth weights; the lazy
value stack (consts, locals, a TOS register, compare entries) and fused compare + branch; frame
overlap; `br_table` jump tables; `if`/`else`; in-place updates and load fusion; the float ops CoreMark
uses (`f64.trunc` gets the SSE2 sequence as the mandatory path; `roundsd` stays as the optional
x86-64-v2 variant, D24). Rewritten: the entry (re-entrant, per convention, no saved-`rsp` unwind),
traps (returned through frames, CET-safe, R2), host calls (one bridge per convention saving every
pool register the convention clobbers, LESSONS 2), stack limits for both stacks, the interrupt poll,
host exceptions, i64 arithmetic, explicit checks for `idiv` overflow (x86 faults on
`INT_MIN / -1`, D10), executable memory (W^X instead of the PoC's RWX mapping).

## Alternatives considered
* **Targets own linking and values** (as the `pyc` adapter does now): each target re-implements
  import checks; cross-target calls need pairwise conversions. Rejected.
* **Python values (signed ints, floats) at the boundary**: loses f32 NaN payloads (S0001) and costs
  the JIT conversions it does not need. Rejected.
* **Per-module fallback with mixed targets in one store** (D5 as first written): needs a memory
  representation shared by `pyc` and the JIT (an `mmap` buffer `pyc` cannot rely on where `ctypes`/
  executable memory are unavailable) and two trap models in one call graph. Rejected by D25.
* **Silent fallback in `--engine jit`**: the JIT's number would include `pyc`'s passes. Rejected.

## Proposed decision: the shape of the shared JIT core
**Options.** (a) LIR materialised as a list with virtual registers, liveness and an allocator
(Cranelift style); (b) one pass over the validated function body, the shared core keeping the lazy
value stack, control stack, pinned locals and labels, and calling a small `MacroAssembler`-like
backend interface per operation (Winch / Liftoff style; what `w2jit2` does, minus the split).

**Measurement** (2026-10-30, after the D24 baseline-ISA change, Xeon @ 2.10 GHz, 4 cores, CPython 3.13.17, one
session, best of 3). Code generation of `w2jit2` on the functions it covers, and a minimal (a)-style
prototype run on exactly those functions: LIR build (operand stack → vregs), liveness (locals
extended over loops), linear scan onto 11 registers, operand rewrite — no live-range splitting, no
fixed-register constraints (x86 division/shifts), no coalescing, no call clobbers: a lower bound.

| module (wasm size; bodies covered by `w2jit2`, functions) | (b) `w2jit2` | (a) = (b) + 4 passes | time added |
|---|---|---|---|
| CoreMark `poc/in/core.wasm` (19 KB; 17 KB, 29/29) | 585–612 KB/s | 338–341 KB/s | +73–80 % |
| `webp_codec.wasm`, playwright-core (552 KB; 81 KB, 254/634) | 436–454 KB/s | 278–289 KB/s | +57 % |
| `resvg.wasm`, claude-code (2.4 MB; 401 KB, 751/1433) | 440–448 KB/s | 270–299 KB/s | +50–63 % |

Commands (from `poc/`): `PYTHONPATH=.. python3 bench/compile/jit_compile_time.py in/core.wasm
<webp_codec.wasm> <resvg.wasm>` (484–508 KB/s); the prototype was a scratch script
(`lir_proto.py`, ~200 lines; to commit under `poc/bench/compile/` if this is accepted). The
prototype's own passes run at 1.8–5.9 MB/s each; together they add 50–80 % to a single pass that
already decodes, chooses pins and emits. A real allocator (splitting, constraints) costs more.

**What (a) would buy is not measured and is bounded**: `w2jit2` is at 80–86 % of Cranelift on
CoreMark (BENCHMARKS.md) and guard pages alone bring it to ~91 % ("Guard pages"): most of the gap
is bounds checks, which (b) addresses by guard pages and bounds-check elimination, not allocation.
Compile time is already 1.5–2.5× Cranelift's wall time ("Compile time on large modules").

**Recommendation: (b).** The shared core (`jit/common/`) reads the validated body once; the
backend interface is the LIR of D16 **as a call interface, not a materialised list**: operations
with operand kinds `Imm | Reg | Slot`, labels, trap points, call sites. To keep a second
architecture from inheriting x86-isms, the interface is three-operand (`binop(op, dst, a, b)`;
x86 moves first), compares are `cmp_branch(cond, a, b, label)` / `cmp_set(cond, dst, a, b)` /
`select_cmp(...)` (no flags in the interface: RISC-V has none), division and shifts are whole
operations (the backend owns `rax`/`rdx`/`cl` and its scratch set), and register roles come from the
backend (`pool`, `scratch`, `tos`). An allocator is added later only if a measurement shows a gain.
**Cost for phase 4**: one backend implementing that interface (~50–70 methods) plus encoder,
trampolines and host bridge for `aapcs64` (or RISC-V psABI); estimated ~1 500–2 000 lines by the
size of `w2jit`/`w2jit2`'s x86-specific code — to be recorded as a measurement then (ROADMAP).
Accepting this needs a new decision in DECISIONS.md (narrowing D16: LIR is an interface) and
an ARCHITECTURE.md "The IR" update; HIR stays the shared layer of `pyc`/`mpy`, and the JIT core
consumes its analyses (pin weights, bounds-check elimination) when they exist, computing pin
weights itself until then.

## Open questions
* None at approval: (b) accepted as D26; `--engine jit` has no fallback and its own xfail file (D25).

## Changes
* 2026-10-30: created (draft).
* 2026-10-30: one target per engine/store, no per-module fallback, pyc/JIT parity (D25).
* 2026-10-30: interrupt polling on by default (timeouts and Ctrl-C are built on it); fuel is an opt-in, later (maintainer).
* 2026-10-30: approved by the maintainer; the proposed decision is D26.
* 2026-10-30: `TargetInstance.limits(name)` for import type checks in `runtime/link.py`; reference values cross the boundary as opaque objects (`None` = null), numbers stay `Raw` (maintainer).
* 2026-10-30: step 3 split into 3a–3d (tables in 3d, native floats in 3c); no Python helpers for computation, only runtime services (D25 amended); `--engine jit-x86_64`, one target adapter, lazy spectest (maintainer).
* 2026-10-30: step 4 done; the fallback warns once (D31).
* 2026-11-01: step 6 done (final review of steps 2–5, its fixes and D34 reviewed); status implemented.
* 2026-11-01 (phase 4, S0004): a second JIT target, `jit-aarch64`; the JIT glue moved to `common/target.py` (`JitTarget`: `arch`, `machines`, `probe_code`, `backend`, `trampolines`). Readiness guard restored: a target with `complete = False` is never chosen by `auto`, which keeps `pyc` with the third D31 message; an explicit engine still runs it. `complete` is read with a default of True (`getattr`: `PycTarget` does not define it, so it stays out of the `Target` Protocol).
* 2026-11-02 (S0005, 5c1): the backend interface gained `simd(f, op, dst, args, imm)` (D39 amendment, D41); `supported` also lists the `0xFD000 + sub` codes a backend compiles. `abi.assemble()` of each architecture returns bytes and `instance.load_trampolines` seals them, so the compiler side imports nothing of the host side.
* 2026-11-02 (S0005, after 5e2): the backend interface gained `move_v128(dst, src)` (second D39 amendment): the core moves v128 pairs whole (locals, constants, `select`).
* 2026-11-02 (S0007 step 3): a fourth operand kind, `AddK` (a pinned i32 plus a constant mod 2³²), accepted only by `move` and as the `load`/`store` address (D39 amendment A).
* 2026-11-04 (S0009 step 2a): required keyword-only `a64` on the eleven memory and table methods and
  `lo` (the declared minimum in bytes) on `load`, `store` and `simd` (D39 amendment B).
* 2026-11-04 (S0009 step 2b): the first claim of the owner is atomic (`StoreState.claim`, under a
  lock); the JIT restores the counters only on the owner thread (a refused entry used to write back
  counters it read before the check, LESSONS 78); JIT `Memory.grow`, `Table.grow` and instantiation
  check it too, with a claim that an entry alone makes permanent (`release`). `pyc` behaviour is
  unchanged apart from the atomic first claim.
* 2026-11-05 (S0012 step 1): `Extern.kind` gains `"tag"`. Until the targets own tag identity
  (S0012 steps 2-3) `link.resolve` builds the value itself, `(exporting instance, tag index)`, checks
  the tag type as a function import's, and never calls `TargetInstance.export` for a tag; the targets
  ignore a tag import when instantiating. A body with `throw`, `throw_ref` or `try_table` is
  `Unsupported("exceptions")` on every target (`supports()`).
* 2026-11-05 (S0012 steps 2-5): supersedes the S0012 step 1 entry above. `TrapCode` gains
  `UNCAUGHT_EXCEPTION = 14` and `Trap` gains `exn: tuple[tag, tuple[Raw | RefVal, ...]]`, set only
  for that code, with the payload as `call` returns values on the target. A body with `throw`,
  `throw_ref` or `try_table` is no longer `Unsupported("exceptions")`: all three targets compile it.
  A tag `Extern`'s `value` is `(exporting instance, tag index)` from `link.resolve`, each target
  resolves it to its own tag; `TargetInstance.export` of a tag returns the target's tag; a tag of
  another target is a `LinkError`.
* 2026-11-11 (S0013 step 1a): `runtime/link.py` checks an import's type by the GC rules: a function
  if the exported function's type is a subtype of the declared one, an immutable global
  covariantly, a mutable global, a table's element type and a tag by type equivalence. Type identity
  is `frontend.types.key(mod, i)`, the iso-recursive canonical key (rec group, position), which
  replaces `functype_key`; the JITs' type ids (`jit/common/instance.type_id(mod, ti)`) use it too.
  A module with GC types is refused by `pyc` and the JITs (`supports()`) until each claims `gc`.
