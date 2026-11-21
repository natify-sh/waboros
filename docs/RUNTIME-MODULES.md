---
title: Runtime modules in wasm (D54) - which parts of the runtime should be one, and in what order
kind: reference
status: draft
updated: 2026-11-16
---

# Runtime modules in wasm: candidates and order

A proposal from the maintainer's discussion (2026-11-10), with the orchestrator's notes and the
maintainer's answers. Besides S0013's collector, which parts of the runtime, built or planned, could
be wasm modules under [D54](DECISIONS.md) (source in the tree, committed `.wasm`, byte-match,
allowed-instruction and structure tests, compiled by our own JIT). Nothing here was built or
measured; estimates are marked as such.

## The criterion

A part of the runtime is a good wasm module when it is:

* **pure computation over memory**: no OS call, no Python object, deterministic (D35);
* **not on the hottest path**: a call into a module costs a few ns, so inline code stays inline;
* **something every host would otherwise write again**: the C loader of D36, MicroPython, hosts in
  other languages. This is the main gain; speed is secondary, since what is hot is already machine
  code.

## Candidates and decisions (maintainer, 2026-11-10)

### With the GC (S0013 step 5), in the collector's module

1. **The subtype check of non-final types** (S0013 Q11): **in the module.** It tests the display of
   the type-info records in the hidden memory, the records the collector reads (built in 5.6). JIT code
   calls the module's function directly, as a call between instances, without entering Python; the
   site stays a safepoint.
2. **Allocation for constant expressions** (`struct.new`, `array.new`, `array.new_fixed` in global
   initializers and element segments): **only the allocation primitive** in the module (`alloc`:
   allocate and initialise an object by its type-info record), so there is one allocator. Evaluating
   the constant expressions stays in Python until item 3.
3. The module's exports, fixed in step 5's plan, are the minimal set a foreign loader may call:
   `collect`, `is_subtype`, `alloc`. **Changed by S0013's "Step 5 decisions"**: `collect` became
   three phases, `begin`, `roots(n)` and `finish` (M3, roots in bounded batches); `alloc` waits for
   the C loader (D7), so Python's `GcHeap` stays the runtime's allocator until then.

**Built (S0013 step 5.2).** `src/waboros/runtime/gc/collector.wat` and the committed
`collector.wasm` beside it, package data loaded as `importlib.resources.files("waboros.runtime") /
"gc/collector.wasm"` (`runtime/gc/` has no `__init__.py`, so no package named `gc` exists). The D54
tie is `tests/test_collector_module.py`: wasmtime-py's `wat2wasm` of the source equals the committed
bytes; our decoder finds the one import `gc.heap`, the exports `begin`, `roots`, `finish` (and
`is_subtype` since 5.6), no data,
no `memory.grow` and only MVP opcodes plus `memory.copy`; and the module collects heap images to the
same bytes on Wasmtime and on our JITs. Its exports and the header-page regions it uses (`ROOTS`,
the handle report) are in S0013 Changes (5.2) and ARTIFACT.md part 3; the runtime uses the module
since step 5.3. **`is_subtype(q, t: i64) -> i32`** (step 5.6) answers whether function type id `q`
is `t` or a declared subtype of it: both records through the tid map at `[60 MiB, 64 MiB)` of the
heap (0 for an id at or past 2²⁰ or without an entry), each checked (in the record area, 8-aligned,
kind 3, depth at most 63, its own display entry; else a trap), then the display test. JIT code
calls it directly through the `IS_SUBTYPE` function record after its own equality test, for a
non-final target only (D61, ARTIFACT.md part 3); the test checks the export list and both engines'
answers.

### Built already, moved for the C loader (with ROADMAP "Next" item 6, not before)

4. **Instantiation work** (today Python, `instance.py`): evaluation of constant expressions
   (`extended-const`, `global.get`, `ref.func`) and the application of active data and element
   segments with their bounds checks and traps in the specification's order. Cold, so no speed gain;
   but a C loader would have to repeat this semantics exactly, and the order of traps is where a
   re-implementation goes wrong (S0012's fuzz found `pyc`'s trap-order bugs in this kind of place).
5. **The four segment libcalls**: `memory.init`, `data.drop`, `table.init`, `elem.drop` (today Python
   services, libcall words 2-5); the same semantics as item 4, so one module serves both. `memory.grow`
   and `table.grow` stay with the host (they allocate from the OS).

**Open for item 6's plan: multi-memory.** `memory.init` and `memory.copy` take a static memory
index, while an instance has any number of memories and C addresses one. Options: exported functions
per memory index up to a limit; a window the host maps for the module; or the module only computes
what goes where and in which order (a list of copies and traps) and the host copies. The last is the
simplest and keeps what matters, the order of the traps.

### Already planned elsewhere

6. **The decoder and the validator** (`binary.py`, `validate.py`): part of the micro-compiler plan
   ([JIT-AS-WASM.md](JIT-AS-WASM.md)). A host that loads artifacts and never compiles does not need it.

### Later phases

7. **Instruction polyfills** for targets without the instructions: `v128` over scalar lanes for the
   sljit track's architectures without SIMD, i64 over i32 pairs on 32-bit targets. Conflicts with D25
   ("libcalls never do computation"). **Decided with the sljit track**, not now; the orchestrator's
   leaning: an exception to D25 for `v128` on targets without SIMD only (large, written once), and i64
   over i32 pairs generated by each backend (hot). Baseline-ISA gaps on x86-64 (`popcnt`, rounding
   without SSE4.1) stay inline.
8. **The computing part of WASI** (phase 6): path resolution and preopen confinement, the layout of
   `stat`/`dirent`, arguments and environment, resource tables; the host keeps only the system calls.
   Wasmtime's `wasi_snapshot_preview1` adapter is such a module already. Overlaps fusedwasi: its
   computing part could become a third twin shared by every engine. Decided with the WASI host
   (ROADMAP, open question 2).
9. **Component-model adapters at run time** (string transcoding, list copies between memories), only
   if components are ever linked at run time; `meld` makes them as wasm at build time.

### Not candidates

* `memory.grow`, `table.grow` (OS allocation); the memory64 move on grow (D50).
* The guard-page fault handler (a signal context, where no wasm runs; it is generated machine code).
* The bridge, the entry trampolines, `tail_switch` (the native ABI).
* `EXN_REFILL` (gone with exceptions in the GC heap, S0013 step 6.3).
* Everything already inline: `memory.copy`/`fill`, exception dispatch, wide arithmetic, relaxed SIMD.
* `atomic.wait`/`notify` (futex), stack switching (native stacks), host calls.
* The `pyc` runtime (Python by definition).
* Canonical type ids: the hash of a rec group is computation, but the type-to-id table is process-wide
  and stays with the host.

## Where a module's own data lives

A runtime module other than the collector has **a small memory of its own** (the JITs support
multi-memory): its data never overlaps an instance's memory, and its accesses are bounds-checked as
any. The collector is the exception: it works on the hidden memory itself, and a C module addresses
one memory (D54), so its data and stack sit in a region of the hidden memory outside every object.

## Order

1. S0013 step 5: items 1-3 in the collector's module.
2. ROADMAP "Next" item 6 (the artifact format, the C loader): items 4 and 5 as one instantiation
   module, with the multi-memory question above.
3. JIT-AS-WASM: item 6, as planned.
4. The sljit track: item 7, after the D25 decision.
5. Phase 6: item 8 with the WASI decision; item 9 only on a need.
