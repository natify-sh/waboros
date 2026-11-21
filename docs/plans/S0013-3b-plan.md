---
title: S0013 step 3b implementation plan
kind: reference
status: outdated
updated: 2026-11-15
---

> The plan for S0013 step 3b, approved by the maintainer (decisions A-O); done 2026-11-15. What was built and decided is in S0013 Changes (3b.1-3b.4) and D57; decision D was reversed and O revised during the step (see there).

# S0013 step 3b: implementation plan (`jit-x86_64` with a store-lifetime GC heap)

Status: PLAN, for the orchestrator's approval. No file of the repository was changed (branch `gc`,
HEAD 656816b, clean).
Read: S0013 (Summary, Values per target, JIT: the GC heap, lowering, safepoints, Roots and the
collector, Context ABI, Artifact, Trap codes, `pyc`, Semantics, Test plan, Plan of steps, Q1-Q13,
Changes 2026-11-11), ARTIFACT.md parts 1-3, D25, D39 (and its amendments), D50, D52, D53, D54,
D56, RISKS R2/R3/R6, LESSONS 1-9, 24, 32, 95, 97, 98, 102, 104, 105; the code:
`jit/common/{compiler,context,gc,walk,instance,lower,backend,simd,target}.py`,
`x86_64/backend.py`, `pyc/{runtime,codegen,__init__}.py` (GC parts), `frontend/types.py`,
`validate._root`/`_SAFEPOINTS`, `tests/test_gc.py`, `tests/spec/{target_engine,claims,test_runner}.py`,
`tools/jit_dump.py`; the 3a plan.

Census (scratch script over the pinned spec cache, this session): 117 modules have `mod["gc"]`;
3 modules without `gc` have a non-final type (`gc/type-subtyping.46-48`), none of them names it
in a `call_indirect`/`return_call_indirect`. 0xFB/`ref.eq` instructions in the suite: casts
(`0xFB14-0xFB19`) 308, `struct.*` 84, `array.*` 140, conversions 14, i31 35, `ref.eq` 6.

## 0. What 3b builds on, and the shape of the answer

* The JIT core is one pass with a lazy operand stack; the only register the core names is `tos`;
  every D39 method takes `Imm | Reg | Slot`; values are zero-extended 64-bit slots.
* `load`/`store`/`simd` address a memory by its **context word**; `lower.bounds` does the
  explicit or guard check. A memory whose word is `MEM0` lives in r13/r14 (aarch64 x26/x25), any
  other is read from its context words at each access. So a heap that is "memory number
  `ndeclared`" needs **no backend change**: every field access is `load`/`store` with that word,
  `a64=False, lo=0`.
* `call_host(f, word, slot)` calls any context word through the bridge: GC services need nothing
  new in the backend either.
* Masks (`mod["refmasks"]`) already exist at every 0xFB instruction and at every call; the core's
  `site()` already turns them into maps of any- and extern-hierarchy slots (`_root`), so "maps for
  every reference type" is already true for code the core emits: 3b only has to keep the
  invariants at the new safepoints (allocation slow path, services).
* **x86-64 backend: only its `supported` set grows** (the 0xFB opcodes and 0xD3). Everything
  else is written once in `common/gc.py` (Q9 (a)). See section 7.

## 1. The hidden memory

**Creation.** One heap per store: `_StoreStacks.heap: GcHeap | None`, created by the first
`JitInstance` whose compiled module has `layout.heap` (section 3: `uses_gc`). New module
`jit/common/heap.py` (runtime; the spec names the heap as a runtime component, and step 5's
collector and roots are its second user):

* `GcHeap.mem = Memory(1, None)`: an ordinary i32 `Memory` (4 GiB + 32 MiB reserved, S0006/D50
  point 4: never moves), page 0 committed (the header area). It is a `Memory` so that step 5's
  collector module can import it like any memory, and so `bind`/`users` publish base and size
  exactly as for linear memories (base before size, S0009). Its `store` is set (thread claim on
  growth, as `Memory.grow`).
* **Commit by range, not by prefix** (open question D): `Reservation.commit_range(off, n)`
  (~6 lines in `execmem.py` over the existing per-OS `_commit(addr, size)`, which already takes any
  sub-range on POSIX and Windows). Regions committed: the header page at creation; the type-info
  area `[64 KiB, 64 MiB)` page by page as records are written; objects from `OBJ = 64 MiB`
  (semispace A's start) in chunks at the first allocation and on growth. `mem.pages` = the end
  of the highest committed range >> 16, published to every bound context (size word). With a
  prefix commit instead, the first object would commit the whole 64 MiB type area (no RSS on
  Linux, but 64 MiB of commit charge per GC store on Windows, where the test suite keeps many
  stores alive).
* In guard mode `guard.add_reservation(heap.mem.res)` (as for i32 memories), so a fault at a fault
  point inside the heap is a trap.
* Header words (u32, little-endian): offset 0..7 zero (null is 0), **cursor at 8, limit at 12**
  (S0013 "Regions"). Initially cursor = limit = `OBJ`: the first allocation takes the slow path,
  which commits the first chunk (Q7: "semispaces committed at the first allocation").

**Where its base lives.** No new fixed word. The heap is bound into each GC-using instance as
**memory index `nmem` (the number of declared memories, imports included)**: its `base` and
`size_bytes` are the two words right after the module's own memory words (S0013 "Context
ABI": "The hidden memory's two words, after the module's memory words (GC modules only)").
`Layout` gains `heap: bool = field(default=False, repr=False)`; `table(i)` becomes
`FIRST_VAR + 2*(memories + heap) + 2*i`; `memory(i)` unchanged; `memories` stays the
**declared** count, so `memory.size`, `memory.grow`, `mem_imm` (the libcall immediates) and the
`memory.grow` service do not see the heap. Consequence worth stating: a GC module with no memory
of its own has the heap as word `MEM0`, i.e. **pinned in r13/r14 for free** (the entry,
`_call_record`, `call_host` and `host_thunk` already load/reload memory 0 from the context).

**ABI amendment.** v1 amended without a bump (Q5, maintainer 2026-11-06, as D34/D50/D52/D56): no
word moves in a module without GC; a GC module's words after its memories shift by two. Recorded
in ARTIFACT.md part 3 and the new decision (section 10).

**Guard vs explicit (Q8 (a)).** Heap accesses are ordinary `load`/`store`/`simd` with
`a64=False`: guard mode leaves them unchecked (offsets < 32 MiB, the address a zero-extended u32)
and marks the instruction a fault point; explicit mode compares against `size_bytes` = the end of
the committed objects range (the type-info area lies below it). No rule special to the heap.

**Memory64 / multi-memory.** The heap is always an i32 memory: the lowering passes
`a64=False, lo=0` whatever the module's memories are; `_Mod.m64/mlo` (declared memories) are not
extended. With several memories the heap's index is `nmem`; libcall immediates unchanged (D37).

**What the artifact records (codes 16-18 and the rest).** Part 3: the hidden memory's two words
(when `uses_gc`), the GC group (section 3), the heap's header words, the object layout, the
type-info record layout (the code reads `kind`, `depth`, `display`), the `externref` encoding, trap
codes 16 `ARRAY_OUT_OF_BOUNDS`, 17 `CAST_FAILURE`, 18 `ALLOCATION_TOO_LARGE` (`base.TrapCode`
has them since step 0; the host-call status set is derived, so a host function may return them
as any trap code). Part 2: nothing new is written (no file exists yet); a note that the GC group's
RTT list (type index per RTT word) is, like the signature list, data the phase-5 format spec must
carry (`Compiled.rtts`, next to `Compiled.sigs`).

## 2. Object layout and type-info records

**Objects** (S0013 "Object layout", fixed there; written down in ARTIFACT.md part 3):
* every object at a multiple of 8: `u32 type-info offset` (its RTT), `u32 collector bits` (0
  while wasm runs; one 8-byte store writes both, from a zero-extended RTT word);
* struct: fields from offset 8 in declaration order, each at its natural alignment: `i8` 1,
  `i16` 2, `i32`/`f32` 4, `i64`/`f64` 8, `v128` 16 (offset aligned to 16 inside the object,
  the object itself only 8-aligned: unaligned SIMD access, D41), any- or extern-hierarchy
  reference 4, `funcref` (record address) 8, `exnref` 8 until step 6; size rounded up to 8, at least
  8 (an empty struct is its header: room for the forwarding word);
* array: `u32 length` at 8, elements from 16 at the element size above (power of two:
  1, 2, 4, 8, 16), total `(16 + n·esize + 7) & ~7`.
* `gc.struct_layout(fields) -> (offsets, size)` and `gc.elem_size(st)` in the core (pure,
  used by the lowering) and by the runtime (records, constant expressions, services): one
  definition (LESSONS 41).

**Type-info records** (proposal; the spec lists the content, not the offsets), u32 words in the
type-info area `[64 KiB, 64 MiB)`, 8-aligned:

| offset | content |
|---|---|
| +0 | `kind` (1 struct, 2 array; 3 func reserved, 0 never: a stray word is not a kind) \| `final << 8` |
| +4 | `depth` d (number of declared supertypes) |
| +8 | struct: byte size; array: element size |
| +12 | `nrefs`: struct: number of traced fields; array: 1 if its elements are traced, else 0 |
| +16 | `display[0..d]`: record offsets of the supertypes by depth, the last its own |
| +16+4(d+1) | struct: the offsets of the traced fields (any- or extern-hierarchy references) |

"Traced" = any- or extern-hierarchy (an `externref` field may hold a GC object); `funcref` and
`exnref` (until step 6) are not. The code reads `+0` (abstract `struct`/`array` casts), `+4` and
`+16 + 4d` (non-final concrete casts); the rest is for step 5's collector. A record never moves.

**Where and when.** `GcHeap.rtt(mod, ti) -> offset`: keyed by `types.key(mod, ti)` in a dict of
the heap (canonical identity across the modules of one store: two modules with equivalent rec
groups get the same offset; inequivalent ones of the same shape at another position get
different keys and offsets); a missing one registers its supertype first (recursion bounded by
the spec's subtyping depth limit, 63), then bumps a type cursor in the type area, commits pages as
needed and writes the record with `struct.pack_into` on a `memoryview` of the heap. Exhaustion of
the area raises `MemoryError("GC type area exhausted")` at instantiation (spec text). Written at
**instantiation**, for every type of the module's RTT list (and their supertypes), before
globals are initialised. Function types get **no record at 3b** (no reader until step 5's
`is_subtype`; open question M).

## 3. The context: the GC group and `uses_gc`

**`uses_gc(mod)`** (core, `gc.py`): `mod["gc"]`, or a `call_indirect`/`return_call_indirect`
whose expected type is not final. Used by `unsupported()` and by `compile_module`:
* `unsupported()`: a module that `uses_gc` and a backend whose `supported` lacks the GC opcodes
  → `"GC types"` (first, as today). So `jit-aarch64` keeps refusing exactly what it refuses now,
  plus a module whose only GC feature is a non-final `call_indirect` type, which it compiles today
  and runs wrongly when the function comes from a subtype in another module (test
  `test_call_indirect_to_a_non_final_expected_type`, module b); none in the spec suite (census).
  Open question K.
* `type_ok`: accepts the GC heap types (`any eq i31 struct array none`, the one-byte forms
  0x6A-0x6E, 0x71-0x73, concrete struct/array indices) when the backend has the GC opcodes.

**Layout** (all new fields `repr=False`, so the D35 lists of modules without GC keep their text):
`heap: bool` (= `uses_gc`), `gcsvc: int` (8 when `heap`, else 0), `rtts: int`. Order of the
variable part: memories, **the heap's 2 words**, tables, globals, imports, libcalls, signatures,
records, tags, **the GC group**: the service words, then one RTT word per entry of
`Compiled.rtts`. `Layout.service(j)`, `Layout.rtt(k)`.

**The GC group** (fixed order, every word present when `heap`, as the libcalls are):

| j | service | slots in → out | allocates (step 5: may collect) |
|---|---|---|---|
| 0 | `ALLOC` | `[bytes]` → nothing (the code retries) | yes |
| 1 | `ARRAY_NEW` (elements wider than a byte) | `[v (1-2 slots), n]` + type → `[obj]` | yes |
| 2 | `ARRAY_NEW_DATA` | `[offset, n]` + type, segment → `[obj]` | yes |
| 3 | `ARRAY_NEW_ELEM` | `[offset, n]` + type, segment → `[obj]` | yes |
| 4 | `ARRAY_FILL` (wider than a byte) | `[arr, i, v (1-2), n]` + type | no |
| 5 | `ARRAY_INIT_DATA` | `[arr, d, s, n]` + type, segment | no |
| 6 | `ARRAY_INIT_ELEM` | `[arr, d, s, n]` + type, segment | no |
| 7 | `FUNC_SUBTYPE` | `[expected type id, record]` → `[0/1]` in slot 0 | no |

Immediates follow the operands in slots, as for the libcalls (D25: services, not computation;
`FUNC_SUBTYPE` is the exception, open question A). Every service follows the rule of the spec:
it reads its reference operands from the slots after any collection it runs; at 3b nothing
collects, but the order is written that way already (operands read after `alloc`).

**RTT words**: `Compiled.rtts` = sorted struct/array type indices that the module allocates
(`struct.new*`, `array.new*`), casts or tests to (`ref.test/cast`, `br_on_cast*` concrete targets),
or needs in a constant expression; `ctx[layout.rtt(k)] = heap.rtt(mod, rtts[k])`. Concrete
**function** cast targets get a **signature word** instead (`Compiled.sigs` grows by them): the
process-wide canonical type id, which is what a function record carries.

## 4. The lowering (`jit/common/gc.py`, called from `compiler._Func.run`)

`run()` sends `0xD3` and `0xFB00 <= op < 0xFC00` to `gc.op(fn, ins, pc)` (as `simd.op`). Helpers:
* `take(fn, n)`: the top n operands, none of them in `tos` (an entry in `tos` is spilled to its
  slot first, a pending compare materialised; LESSONS 32), then `need_tos()`: afterwards `tos`
  is free scratch and the operands are `Imm | Reg(pin) | Slot`. Used by every non-allocating
  instruction (no flush: locals and constants are read where they are).
* **Scratch slots**: `fn.L + h + k` above the current height (h before the pops, so operand slots
  stay intact), `maxh` raised. The lowering has one register (`tos`); a second value lives in a
  scratch slot (x86 compares and stores take a slot operand directly). Open question C.
* **Cold traps**: `fn.trap_label(code)`: one label per code per function, bound after the body
  (next to the landing pads) as `bind; b.trap(f, code)`. A check is `cmp_branch(cond, ..., label)`:
  the hot path falls through; no new backend method (a trap stub keeps `unwind`, as today).
* **Cold calls**: allocation slow paths and other cold service calls are recorded during the pass
  `(label, retry, pc, f.error, slot, keep, top)` and emitted after the body, before the landing
  pads, with `f.error` set to the value it had at the instruction (a slow path inside a
  `try_table` reports its status to that pad, which passes every status but 14 on), and with
  `fn.site(pc, 0, keep, top)` right after each `call_host`, so `b.sites` and the core's maps stay
  paired in code order (the 3a assertion checks it).
* **Null checks**: `null_check(f, x, NULL_REFERENCE)` on every access, nullable or not (the core
  does not track operand types; open question B).

**Per instruction** (operands in wasm order; "hw" = the heap's context word `lay.memory(nmem)`):

| instruction | lowering (D39 methods only) |
|---|---|
| `struct.new t`, `struct.new_default t` | allocating (flush first: a safepoint, I2): `alloc(t, Imm(size))`, then one `store(size_k, tos, off_k, Slot(field k))` per field (`_default`: `Imm(0)`; v128: `simd` v128.store from the pair / two 8-byte zero stores); the object in `tos` |
| `struct.get[_s/_u] t k` | `take(1)`; null check; `load(size, signed, wide, tos, x, off)` (packed: 1/2 bytes, `_s` sign-extends, `_u` zero-extends; i32/f32 4, i64/f64/funcref 8, any/extern ref 4 zero-extended; v128: `simd` v128.load into a new slot pair) |
| `struct.set t k` | `take(2)`; null check; `store(size, x, off, v)` (packed stores truncate) |
| `array.new_default t n`, `array.new t v n` (1-byte elements) | flush; `n >u ALIM[wsize]` → `ALLOCATION_TOO_LARGE`; size = `(16 + n<<log2(esize) + 7) & ~7` in 64-bit (`binop` wide into `tos`, kept in a scratch slot); `alloc(t, Slot(size))`; `store(4, tos, 8, n)`; `memory_fill(f, obj+16, 0 or v, n·esize, hw)` (the bulk method writes `tos`: the object is kept in a scratch slot and reloaded) |
| `array.new t v n` (wider elements) | service `ARRAY_NEW` (the spec's rule: a fill of wide elements is a service) |
| `array.new_fixed t N` | flush; `alloc(t, Imm(size))`; length; N stores |
| `array.new_data`, `array.new_elem` | service (segment bounds `MEMORY`/`TABLE_OUT_OF_BOUNDS` before the allocation, never too large: LESSONS 102) |
| `array.get[_s/_u] t`, `array.set t` | `take`; null check; `len` = `load(4, .., x, 8)` into `tos`; `cmp_branch(ge_u, i, tos, ARRAY_OOB)`; `tos = i << log2(esize)` (32-bit `binop shl`), `tos = tos + x` (32-bit `binop add`): the address is a u32 whatever the length (R3); `load`/`store` at offset 16 (v128: `simd`) |
| `array.len` | `take(1)`; null check; `load(4, .., x, 8)` |
| `array.fill t` | `take(4)`; null check; `i + n >u len` (64-bit add, no wrap) → `ARRAY_OOB`; 1-byte elements: `memory_fill` on the heap; wider: service `ARRAY_FILL` (flush: a safepoint) |
| `array.copy td ts` | `take(5)`; Wasmtime's order: dst null, dst range, src null, src range (LESSONS 102); addresses `obj + 16 + i·esize` formed in 32 bits into scratch slots, byte count `n·esize`; `memory_copy(f, d, s, nbytes, hw, hw)` (memmove; no barrier needed) |
| `array.init_data`, `array.init_elem` | service: array null, array range, segment range (LESSONS 102) |
| `ref.i31` | `binop shl tos, x, 1` then `binop or tos, tos, 1`, 32-bit (keeps the low 31 bits) |
| `i31.get_s`, `i31.get_u` | null check (`NULL_REFERENCE`, Wasmtime 50); `binop shr_s / shr_u tos, x, 1`, 32-bit |
| `ref.eq` | `push((CMP, ("eq", x, y, False)))`: a lazy compare like `i32.eq`, so `br_if`/`if`/`select` fuse it (words are zero-extended u32) |
| `any.convert_extern`, `extern.convert_any` | nothing: the entry stays (identity, S0013 "Values per target") |
| `ref.test[ null] ht` | `take(1)`; `test(x, ht, null, fail)`; `tos = 1`, jump; `fail: tos = 0` |
| `ref.cast[ null] ht` | the operand stays on the stack (in a slot or local); `test(x, ht, null, CAST_FAILURE label)` |
| `br_on_cast`, `br_on_cast_fail` | flush (a branch); the operand's slot; `test` with the miss label; then the `br_if` pattern (`needs_moves`, `moves`, `jump`, skip) on the hit (or the miss) |

**`alloc(t, size)`** (the inline fast path; the spec's sequence):
```
ctx_load(tos, rtt word of t); move(S_rtt, tos)               # the RTT (a type-area offset: not a reference)
retry:
load(4, tos, Imm(0), 8, hw)                                  # cursor
binop add tos, tos, size (64-bit); move(S_new, tos)          # cursor + size: no wrap even for 4 GiB arrays
load(4, tos, Imm(0), 12, hw)                                 # limit
cmp_branch(gt_u, S_new, tos, slow) (64-bit)
store(4, Imm(0), 8, S_new, hw)                               # cursor = new
binop sub tos, S_new, size                                   # the object
store(8, tos, 0, S_rtt, hw)                                  # header {rtt, bits 0}
...field stores from the operand slots; the object stays in tos (store preserves tos)
cold: slow: move(S_arg, size); call_host(f, ALLOC word, S_arg); site; jump retry
```
The slow path is a `call_host` site: its map covers the locals and every operand of the
instruction (`struct.new`'s fields, `array.new`'s value; the validator's mask at that `pc`), as the
spec's "for a service call, h includes the service's argument slots" asks; scratch slots hold no
reference. After it, the code reads everything again from slots (I4): nothing derived from a
reference survives the call. The service at 3b: commit more of semispace A (at least the request,
at least 1 MiB, doubling up to the end of A), set the limit; if A cannot hold it, status 13 with
`MemoryError("GC heap out of memory")` pending (spec "Semantics"; D34).

**`test(x, ht, null, fail)`** (casts; the source hierarchy from `types.top(mod, ht)`):
* null: `cmp_branch(eq, x, 0, null ? hit : fail)` (a non-null target never accepts null;
  `ref.cast` non-null on null is `CAST_FAILURE`, as `pyc`);
* `any`/`extern`/`func`/`exn`: non-null; `none`/`noextern`/`nofunc`/`noexn`: null only;
* `eq`: `(x & 3) != 2` (i31 `x1`, object `00`; a host word `10` is not `eq`); `i31`: `x & 1`;
* `struct`/`array`: `x & 3 == 0`, then `tos = load(4, x, 0)` (the record), `load(4, tos, 0) & 0xFF`
  against the kind;
* a concrete struct/array type T: `x & 3 == 0`; `tos = header`; final T: `cmp_branch(ne, tos, RTT_T)`
  (RTT in a scratch slot from `ctx_load`); non-final: record in a scratch slot,
  `load(4, tos, 4) <u d` → fail, `load(4, record, 16 + 4d) != RTT_T` → fail (the display at the
  target's depth; Q11 does not apply to struct/array types: always inline);
* a concrete **function** type: non-null, then service `FUNC_SUBTYPE(sig word, record)` (Q11,
  until step 5) and `cmp_branch(eq, result, 0, fail)`; a safepoint (map as any service).
Each tag test uses `binop and` into `tos` and `cmp_branch`; nothing else.

**Non-final `call_indirect` / `return_call_indirect`** (compiler.py, `uses_gc` modules only; a
final expected type keeps today's backend method and bytes): `table_get(tos, index)` (bounds:
`TABLE_OUT_OF_BOUNDS`, as the backend's own check), `null_check(tos, INDIRECT_CALL_TO_NULL)`,
record and expected id into two scratch slots above the arguments, `FUNC_SUBTYPE` →
`cmp_branch(eq, 0, BAD_SIGNATURE)`, then `call_ref(f, Slot(record), L + h0)` (or `tail_ref`).
The trap order is `call_indirect`'s (bounds, null, signature). Both calls are sites with their maps.

**Trap codes and order** (Semantics; LESSONS 95, 102): operands are evaluated before the
instruction (lazy entries hold values only); then null, then bounds, then the access; casts fail
with 17; arrays: too large (18) before allocation, segment bounds before allocation; `array.copy`
dst null, dst bounds, src null, src bounds. The ALIM bound is Wasmtime's per **Wasmtime's**
element size (a reference 4 bytes, `funcref` too), so the JIT traps at exactly `pyc`'s lengths
even where its own element is 8 bytes (an 8-byte `funcref` array past the heap is "GC heap out of
memory", as an array between 2 and 4 GiB is, spec "Semantics"). `pyc`'s `_ALIM` moves to
`targets/base.py` (`ALLOC_LIMIT`) and both targets import it (open question J).

**Packed fields**: `load` size 1/2 with `signed` for `_s`, `store` size 1/2. **v128 fields**:
`simd` with op `0xFD000`/`0xFD00B`, `imm = (offset, hw)`, `a64=False, lo=0`; the GC scratch slots
are placed so they never overlap the SIMD backend's scratch slots (`base + 6..`, checked when
writing: a risk listed in section 10).

## 5. Constant expressions that allocate

**Who**: the runtime, Python, at instantiation (`JitInstance._const`), with the same layout
functions as the core (section 2) and the same allocator as the services (`GcHeap.alloc(size)`:
bump the header cursor, the slow path's growth when full; never a collection, S0013 "Roots": "the
runtime's own allocations grow the heap and never collect"). Step 5 replaces the body of `alloc`
with the collector module's `alloc` export (Q4, RUNTIME-MODULES.md); the callers do not change.

**How**: `("gc", ins, operands)` with an explicit stack, operands first, no recursion per nesting
level (the 10 000-deep test of step 2 runs on the JIT too): `struct.new`/`_default`,
`array.new`/`_default`/`_fixed` (writing every field and element, defaults included: the
collector never zeroes), `ref.i31` (`(v << 1 | 1) & 0xFFFFFFFF`), the two conversions
(identity). `array.new` past `ALLOC_LIMIT` raises `Trap(ALLOCATION_TOO_LARGE)` from
instantiation, as `pyc`.

**Order** (step 2's review finding): globals in order; each table initializer once for the whole
table; then **every element item of every segment** (the existing list comprehension already
evaluates all of them before the first active write, which comes after); then active element
writes, active data, `start`. Unchanged from today's order, with a test on the JIT (`test_gc.py`
`test_element_expressions_run_before_any_active_write`, now parametrised over `jit-x86_64`).

## 6. `externref` re-encoding, the handle table and the host boundary

**Encoding** (runtime only): an extern-hierarchy host value is `(h << 2) | 2` with `h` the store's
1-based handle number (as today's `h`); `to_raw` refuses more than 2³⁰ − 1 host values
(`MemoryError`), so a host word fits the 4-byte reference fields. `walk.check`'s handle test
becomes the word classification below.

**Proof that existing code bytes do not change** (identity): no JIT instruction reads an
externref's bits except the null test (`ref.is_null`, `br_on_null`, `ref.as_non_null`,
`call_ref`'s null check on funcrefs only), and `ref.null extern` is 0 in both encodings; the
encoding lives in `_StoreStacks.to_raw/from_raw` only. Checked by the D35 lists (section 9): every
line that compiles before 3b is unchanged on both JITs, guard and explicit.

**Hierarchy by type, not by byte**: today `isextern(t)` decides with one-byte codes, so `anyref`
(0x6E) and a concrete struct reference would fall into the funcref branch of `to_raw`. `to_raw` and
`from_raw` take the module (`types.top(mod, nn(t)[1])`); callers: `JitInstance.call`, `_ref_host`
(the importing module), `global_get`, `_uncaught` (`Tag` gains its module). ~10 lines of plumbing.

**Host view** (minimal, the public shape is phase 6):
* any hierarchy: null `None`; i31 a Python **int** (the signed value, as `pyc`); an object a
  **`GcRef`** (frozen: store, handle index, `kind` 'struct'/'array' read from its record at
  creation); a host value **the host object itself** (open question E: `pyc` hands out its `_Host`
  box there).
* extern hierarchy: null; a host value the object; anything else (a GC object or an i31 that went
  through `extern.convert_any`) a `GcRef` (kind 'i31' for an i31 word), so it round-trips.
* host → raw, any hierarchy: `None` 0; a `GcRef` of this store its word (another store's, or a
  forged one: `TypeError`, LESSONS 35); an `int` an i31 **only if `-2**30 <= v < 2**30`, else
  `ValueError`** (the carried-over range check, also in `pyc`'s `_host_any`; open question G);
  anything else internalized as `any.convert_extern` would (the host word). Extern hierarchy:
  `GcRef` → its word, `None` 0, anything else a host word.

**The handle table** (`_StoreStacks.gc_handles: list[int]` of words, a dict word → `GcRef` so one
object is one handle and `from_raw` returns the same `GcRef`): a root at step 5 (the collector
rewrites the words, then the dict is rebuilt). Kept until the store is dropped (Q10, the deviation
recorded at 3b). Generation counts and the free list: open question F.

**Host values written into anyref globals or tables outside a call** (carried over): the JIT has
no host path that writes a global or a table (`GlobalCell` and `Table` have no host setter; the
spec harness's `spectest` is a wasm module), so nothing to internalize at 3b: phase 6, Q10.

## 7. Stack maps for every reference type; the walk check over GC words

**Maps**: already built from the validator's masks for any- and extern-hierarchy references,
concrete struct/array types included (`_root`), `funcref`/`exnref` excluded (exnref from step 6),
v128 two slots. 3b adds the new safepoints: the `ALLOC` slow path and every GC service (shift 0,
the service's argument slots included), the `FUNC_SUBTYPE` site, and the `call_ref` of a
non-final `call_indirect` (as `call_indirect`'s). The core's existing assertions (the mask has no
bit above the operand height; one core map per backend site, per function) cover them. I1 is
unchanged (pins are i32/i64 only).

**Walk check** (`WABOROS_JIT_WALK_CHECK=1`): every marked slot must hold a valid word:
0; odd and < 2³² (i31); `w & 3 == 2` and `1 <= w >> 2 <= len(externs)` (host); `w & 3 == 0`
and `OBJ <= w < cursor`, `w % 8 == 0`, and the u32 at `w` a registered record offset (object).
Anything else is the existing `RuntimeError`. Every GC service call runs the check in check mode
(services go through `_host`, which `_checked` wraps), so at 3b the maps of every allocation site
in the spec suite are validated against real words: a mark on a non-reference slot shows up as
an invalid word as soon as that slot holds a non-reference value.

## 8. What the x86-64 backend needs, and what aarch64 will

**x86-64**: its `supported` set gains `gc.OPS` (0xFB00-0xFB1E, 0xD3). Nothing else: the lowering
uses `move`, `binop`, `cmp_branch`, `load`, `store` (Imm/Reg/Slot addresses, Slot sources),
`simd` (v128 load/store), `ctx_load`, `null_check` (codes 7, 10), `table_get`, `memory_fill`,
`memory_copy`, `call_host`, `call_ref`, `tail_ref`, `trap`, labels; all exist with every operand
kind on x86-64 (read: `_cmp`, `binop`, `move`, `_bounds`). No `ll_*` op is added (S0011 unchanged).
If the measurement (section 9) shows the slot-scratch sequences too slow, a D39 or `ll_*`
addition is its own amendment later.

**aarch64 (step 4)**: the same set added to `supported`; then check, under QEMU and native, the
operand combinations above (a `Slot` address and a `Slot` store source on a non-zero memory and on
memory 0 = the heap in x26/x25; `cmp_branch` Slot/Slot and Slot/Reg; 64-bit `binop` with a Slot;
v128 `simd` load/store on the heap; `table_get` then `call_ref`/`tail_ref` from a slot); far mode:
the core's cold labels and records are per `_Func` run, so a `rewind` + far re-run rebuilds them
(and `rewind` already drops the backend's sites).

## 9. Claims, tests, mutations, identity, ISA gate, measurements

**Claims**: `tests/spec/claims.py`: `jit-x86_64` gains `gc`; `xfail/jit-x86_64.txt` loses its 57
"unsupported: GC types" lines (expected empty, as `pyc`'s). The D25 parity test needs no code
change: it already drops the exemption for a JIT that claims `gc` (`claimed(jit, "gc")`), so x86
is compared whole and `jit-aarch64` keeps ignoring exactly its "GC types" lines until step 4.
Consistency with D25's `auto` rule: since step 2 `auto` selects `jit-aarch64` (`complete`) while
its GC lines differ from `pyc`'s, under the recorded exemption "until steps 3b-4" (D52 text,
TESTING.md); 3b narrows it to aarch64 and step 4 removes it. Proposal: keep it as is, and reword
the D52/TESTING sentence to "jit-aarch64 until step 4". The alternative, `complete = False` for
aarch64 until step 4, would drop arm64 users to `pyc` for every module (D25: never per module),
a regression for no gain.

**Tests** (~250 lines):
* `tests/test_gc.py`: `jit-x86_64` enabled in `TARGETS` (every test of step 2 runs on it), the
  host-view differences handled by one helper (`r.v if pyc box else r`, open question E);
  new cases: the i31 host range (`2**30` → `ValueError`, both targets); a heap volume test (more
  than the first chunk of objects, a list of 10⁵ structs walked back: catches a fast path that
  ignores the limit or a slow path that does not retry); a struct and an array with every storage
  type at its offsets read back through `array.copy` (layout); `externref` → `anyref` → `externref`
  of an i31 and an object (the `GcRef` round trip).
* `tests/jit/test_gc_heap.py` (new, x86-64): two modules with equivalent rec groups in one store
  share record offsets, an inequivalent one at another position does not; the header words after
  an allocation; the type-info record of a depth-3 type (display); the hidden memory index after a
  memory64 and after two declared memories (field access works, `memory.size` of each unchanged);
  guard and explicit (`WABOROS_JIT_BOUNDS`); a GC module with no memory of its own (heap = memory
  0); the walk check's word classification (a valid object, an i31, a host word pass; a misaligned
  word, a word past the cursor fail).
* `tests/jit/test_stackmaps.py`: exact maps of a GC function: a struct local and an anyref
  parameter at a call; a `struct.new` slow path (the fields marked, the scratch slots not); an
  `array.new` value operand at its service; a `call_indirect` to a non-final type (both sites).
* I1 with heavily used `anyref`/`(ref $s)` locals: no pin, all marked.
* `tests/spec/target_engine.py`: classify the JIT's values (`GcRef.kind`, an int as i31, a host
  object as `HostRef`); `tests/spec/test_runner.py` unchanged.

**Mutations of the lowering** (scratch driver from 3a: a fresh `git archive` copy per mutant, its
own session, the process group killed on timeout, a crash or hang counted as caught; runner
`tests/test_gc.py -k jit` plus the spec files `gc/{struct,array,i31,ref_test,ref_cast,br_on_cast,
br_on_cast_fail,type-subtyping,ref_eq,array_copy,array_fill,array_init_data,array_init_elem,
array_new_data,array_new_elem,extern}.wast` on `jit-x86_64`, guard and explicit, walk check on).
Each must be caught:
* a cast to a non-final type by record equality; the display read at depth − 1; the depth test
  `le_u` for `lt_u`;
* a null check dropped (struct.get; array.len; i31.get_s);
* array bounds with `gt_u`; `array.fill` range without its `+ n`; `array.copy` src range against
  the dst length (LESSONS 97 sizes);
* `get_s` and `get_u` swapped (struct and array);
* the element address formed in 64 bits with the length check removed (an index of 2³² − 1:
  crash in guard, trap in explicit, both caught);
* the allocation fast path ignoring the limit; the slow path not retrying (volume test);
* a wrong field offset (alignment of `i64` after `i8` ignored); the header written without the
  RTT;
* `ALLOC_LIMIT` off by one; `array.copy` trap order (src null before dst bounds);
* `externref` encoded as `h` in `to_raw` only (round trip);
* a `_default` allocation that skips its field stores: **not observable at 3b** without poison
  (fresh pages read zero, nothing is reused; LESSONS 105), unless the free-space poison of step 5
  comes now (open question I).
Not at 3b (no object moves until step 5): a missing map bit, a stale slot, a service that reads
its operand before collecting; the exact-map unit tests cover the first.

**Identity** (D35, LESSONS 98): `git archive HEAD src tests tools poc/in` into a scratch tree, the
spec cache linked in; `tools/jit_dump.py` in that tree and in the working tree for `jit-x86_64` and
`jit-aarch64`, `WABOROS_JIT_BOUNDS=guard` and `explicit` (8 lists). Pass: `jit-aarch64` lists
equal line for line; `jit-x86_64` lists: every line that compiled before is equal (all four
columns), the only changed lines are the "unsupported: GC types" ones, which now carry digests
(the count printed; expected 117, the modules step 1b moved to "GC types"), and the after-tree shows the change (proves which tree ran). `jit_dump.py` adds `c.rtts`
to its digest only when non-empty (lines of other modules keep their text). CoreMark's line equal.

**ISA gate**: `uv run python tools/isa_all.py` (x86-64; GC modules included now) clean; aarch64
unchanged.

**Measurements** (BENCHMARKS.md, conditions as usual: dev VM, `taskset -c 2`, alternated, medians
and ranges):
* CoreMark on `jit-x86_64` guard: identical code, so only a self-check and 3 alternated pairs
  against HEAD to show no runtime-side effect;
* a host-call loop with an `externref` argument (the re-encoding and the hierarchy lookup in
  `to_raw`), alternated with HEAD;
* the GC microbenchmarks of the spec's Measurements, now real: churn (`struct.new` + `struct.get`,
  10⁷ iterations: 160 MB of heap, no reclaim before step 5) and list (build and walk 10⁶ nodes), on
  `jit-x86_64` guard and explicit, `pyc` and Wasmtime, alternated in one session, against the
  spec's lowered-shape numbers (6.6 and 9.6 ns); a cast loop (final, non-final depth 3) as well.
  A small `tools/bench_gc.py` (~80 lines, as `bench_throw.py`; steps 5-7 reuse it).

## 10. Sub-steps

| sub-step | content | acceptance | budget (src / tests) | risk |
|---|---|---|---|---|
| 3b.1 | Runtime and layout, no lowering yet (the JITs still refuse GC): `heap.py` (`GcHeap`: reservation, range commit, header, records, `rtt`, `alloc`, growth), `Reservation.commit_range`; `Layout.heap/gcsvc/rtts` (repr-stable), `uses_gc`, `Compiled.rtts`; the heap bound in `JitInstance`, guard registration, RTT and service words; `externref` re-encoding, `to_raw`/`from_raw` by hierarchy, `GcRef`, handle table, the i31 range check (`pyc` too), `ALLOC_LIMIT` in `base.py`; the walk check's word classification | `uv run pytest -q` (all); identity: the 8 D35 lists equal line for line before/after (nothing compiles differently yet: proves the re-encoding and the layout fields leave code alone); spec `jit-x86_64` guard with `WABOROS_JIT_WALK_CHECK=1`: 63 375 pass, 57 expected failures, as before; unit tests of records (canonical offsets) and encodings | runtime ~190, core ~35 / ~60 | low-medium: runtime only; the hierarchy fix touches every reference crossing the host boundary |
| 3b.2 | The lowering, part 1: `gc.op`, `take`, cold traps and cold calls, `alloc` + `ALLOC`, structs, i31, `ref.eq`, conversions, `test` for every target kind, `ref.test/cast`, `br_on_cast*`, `FUNC_SUBTYPE`, the non-final `call_indirect`/`return_call_indirect`; struct/i31/conversion constant expressions; x86 `supported` += those opcodes (a module with an array opcode still says `unsupported: opcode 0xfb..`) | `tests/test_gc.py -k "jit and not array"`; with `gc` claimed locally (not committed): the spec `gc/` files without array instructions pass on `jit-x86_64` guard and explicit, walk check on, and nothing else changes; `tests/jit/test_stackmaps.py`; ISA gate | core ~290, runtime ~60 / ~90 | **high (R3)**: allocation arithmetic, field offsets, the lazy stack around scratch use (LESSONS 32), cold code and `f.error` |
| 3b.3 | Arrays: `new*`, `get/set/len`, `fill`, `copy`, `init_*`, services 1-6, array constant expressions, `ALLOCATION_TOO_LARGE`; x86 `supported` complete; claim `gc`; xfail file; adapter classification | spec `jit-x86_64` guard and explicit with `WABOROS_JIT_WALK_CHECK=1`: at parity with `pyc` (64 229 pass, 0 expected failures, 0 stale; `gc/` 797 pass, 1 skip); `uv run pytest -q` incl. the D25 parity test; identity (section 9); `tools/isa_all.py` clean | core ~170, runtime ~90 / ~80 | **high (R3)**: element addresses, bulk ranges and trap order (LESSONS 97, 102) |
| 3b.4 | Mutations, measurements, docs | every listed mutant caught (or stated unobservable); BENCHMARKS rows; `python3 tools/check_docs.py`; `python3 tools/features.py --check` | tools ~80 (`bench_gc.py`), docs | — |

**Total**: src ~835 (core ~495, runtime ~340) and tests ~230, against the spec's ~750 (core ~450,
runtime ~300) and ~250: **about 85 lines (11 %) over**, mostly the casts (every abstract kind,
three hierarchies), the non-final `call_indirect` path the spec did not count, and the host-boundary
hierarchy fix. Each sub-step stops and reports at +20 % of its own budget.

**Docs** (3b.4; STANDARDS "Which document to update"):
* ARTIFACT.md part 3: the hidden memory's two words (after the memories, GC modules only; a GC
  module without memory has it as memory 0), the GC group (service order and slot conventions,
  RTT words), the heap header, object and type-info record layouts, the `externref` encoding and
  the 4-byte limit on host handles, codes 16-18 in the trap table; part 2: the note on the RTT list;
  `updated:`.
* DECISIONS.md: a new decision (D57): the JIT GC heap as a hidden i32 memory per store, u32
  references and the tag bits, the GC group appended after the tags, records keyed by
  `types.key`, `externref` as `(h << 2) | 2`, ABI v1 amended without a bump (Q5), handles kept
  until the store is dropped (the Q10 deviation), `FUNC_SUBTYPE` in Python until step 5 (if A is
  approved); D52's sentence on the parity exemption; D39 line: nothing changes (opcodes only).
* ARCHITECTURE.md "The IR": GC lowered once in the core (`common/gc.py`) over D39 methods; line 132.
* S0013: Progress, Changes (interface details beyond the text: record offsets, the service list,
  `uses_gc`, the non-final `call_indirect` lowering, the host view, scratch slots).
* TESTING.md (x86 xfail counts, the GC tests); FEATURES.md through `tools/features.py`; TARGETS.md
  and WASM-PROPOSALS.md (the GC row: x86-64 yes); BENCHMARKS.md; LESSONS.md for any trap met.

## 11. Open questions (options and costs; nothing chosen silently)

* **A. Function subtyping is a Python service at 3b.** `FUNC_SUBTYPE` (non-final `call_indirect`
  and every concrete-function cast) is a computation, which D25 does not let the JIT hand to
  Python; the spec moves `is_subtype` into the collector module at step 5. (a) Python service now,
  recorded as a temporary deviation until step 5 (~25 lines; ~5 µs per such call: rare, the
  spec suite and the measured toolchains use typed `call_ref`); (b) a D39 amendment that lets the
  core read a record's type id inline, so equality is inline and only a miss calls the service
  (Q11 (a) as written): both backends in the same step (D39), so aarch64 code in 3b. **Recommend
  (a).** Note: even the final-type function casts go to the service under (a), since the core
  cannot read a record word.
* **B. Null checks always.** The core does not track operand types, so "omitted when non-nullable"
  needs a per-instruction nullability bit from the validator (a third mask, ~15 lines in 1b's
  code). Cost of always checking: `test; jcc` per access. **Recommend always**, revisit with the
  benchmark.
* **C. One register.** The lowering has only `tos`; a second value goes through a scratch slot
  (2 extra memory operations per cast or allocation). (a) slots now, measured by `bench_gc.py`;
  (b) a second core-visible scratch register (a D39 change). **Recommend (a).**
* **D. Range commit** (`commit_range`, ~6 lines) as the spec describes, vs a prefix commit (the
  first object commits the 64 MiB type area: commit charge on Windows). **Recommend range.**
* **E. Host view of a host value in an anyref**: the JIT gives the object itself, `pyc` its
  `_Host` box; the test `test_a_host_value_for_an_anyref_is_internalized` asserts the box. (a)
  unboxed on the JIT, the test unwraps either; (b) a shared box class in `targets/base.py` used by
  both (touches `pyc`). Phase 6 fixes the public shape. **Recommend (a).**
* **F. Handle table extent.** Q10 says "index, offset, free list, generation" at 3b; with no
  release API before phase 6 the free list and generations have no user. (a) index + word + the
  dedup dict now, the rest in phase 6 with `unroot`; (b) all now (+~12 lines, untested paths).
  **Recommend (a)**; needs the orchestrator's (or maintainer's) OK since the spec text says 3b.
* **G. i31 from a host int**: `-2**30 <= v < 2**30` else `ValueError`, both targets (`pyc`'s
  `_host_any` gets the same check: a correctness fix of a frozen target); alternatives: wrap like
  `ref.i31`, or accept `0 <= v < 2**31` too. **Recommend the signed range**, as the value the host
  gets back.
* **H. `array.new` with wide elements**: a service (spec) vs inline allocation + `ARRAY_FILL`
  (no new service, but a window where a fresh object with unwritten elements sits in an unmarked
  scratch slot across a call). **Recommend the spec's service.**
* **I. Poison of fresh heap space at 3b** (`WABOROS_JIT_GC_POISON=1` filling `[cursor, limit)`
  when the service commits, ~8 lines): makes the `_default` mutation catchable now; else it waits
  for step 5. **Recommend now** (the switch is the spec's, only its free-space half).
* **J. `ALLOC_LIMIT` in `targets/base.py`**, imported by `pyc`'s runtime (it already imports
  `base`) and the JIT core, vs a copy with a test that they agree. **Recommend base.py.**
* **K. aarch64 and non-final `call_indirect` types**: refuse such a module ("GC types") until step
  4, though it compiles today (wrongly for a subtype from another module; none in the suite), vs
  letting aarch64 take the new core path untested until step 4 (its D35 lists would still not
  change). **Recommend refuse.**
* **L. Host writes into anyref globals/tables outside a call**: no JIT path exists; phase 6.
* **M. Type-info records for function types**: not written at 3b (no reader before step 5) vs
  written now as the spec lists "kind (struct, array, func)". **Recommend defer to step 5.**
* **N. `exnref` fields and elements are 8 bytes until step 6** (an arena address); the allocation
  limit still uses Wasmtime's 4. Documented in ARTIFACT.md part 3.
* **O. No type check of a `GcRef` the host passes for a concrete reference parameter** (Wasmtime
  checks): memory-safe (the word is a real object, every access is bounds-checked or guarded), a
  wrong type gives wrong values, not a crash. Phase 6.
* **P. Budget**: ~11 % over the spec's size (section 10). Proceed, or cut: drop `tools/bench_gc.py`
  (use a scratch script), or postpone the non-final `call_indirect` path (then the module-b case of
  `test_gc.py` stays a known failure on the JIT: not acceptable for parity). **Recommend proceed.**

## 12. Invariants this step relies on and keeps

* I1 (no reference pinned), I2 (operand stack in slots at every call: every allocating instruction
  and service flushes first), I3 (marked slots hold valid words: checked by the walk check over GC
  words), I4 (nothing derived from a reference kept across a call: the slow path retries from the
  slots).
* LESSONS 1 (flags): only fused D39 compares; 5 (zero-extension): u32 loads and 32-bit address
  arithmetic; 7 (memory never moves): the heap is an i32 memory; 32 (`tos` still holding a popped
  operand): `take()`; 95/102 (trap order); 97 (distinct operand sizes in the tests); 98 (identity
  from a full copy); 104 (any hand-built context: the heap words exist only in GC layouts, so
  `tools/stack_info.py` and the ABI tests need nothing; grep for `Layout(` and `ctx[` users anyway).
* R2: no new trampoline, no indirect branch target, no new instruction form; R3: every heap access
  is `lower.bounds` (explicit) or a fault point (guard); addresses are u32; the allocation sum is
  64-bit; host words fit 4 bytes.
