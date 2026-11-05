---
title: JIT low layer - wasm memory semantics written once over per-architecture operations
kind: spec
id: S0011
status: implemented
superseded_by:
related: [D24, D26, D35, D39, D44, D45, D47, R3, S0004, S0006, S0007, S0009]
owner: maintainer
updated: 2026-11-05
---

# S0011 JIT low layer: wasm memory semantics written once over per-architecture operations

## Summary
The D39 backend interface ([DECISIONS.md](../DECISIONS.md)) is at the level of wasm semantics, so each
backend implements the semantics as well as the encoding (LESSONS 76). For example, S0009 step 2a wrote
the i64 bounds check twice. This spec moves one group of semantics into one shared layer,
`jit/common/`. That layer drives a small set of per-architecture operations: the **low layer**, the
`ll_*` methods of each backend. The group is the bounds checks of memory accesses (loads, stores,
SIMD memory operators; i32 and i64; guard and explicit mode), plus the end checks of bulk memory and
tables (`_end_check`) and the table element check (`_element`). The low layer becomes a contract
between the shared layer and every JIT architecture, which is why it needs a spec. GC will be written
on top of it.

**Evidence: the reconnaissance (ROADMAP "Next" 3b, 2026-11-04).** A scratch prototype covered the
memory bounds checks only, not `_end_check` or `_element`. Its parts:
* a shared `bounds()` of 84 lines;
* low layers of about 100 lines on x86-64 and about 65 on aarch64;
* a hook in each backend's `_bounds`, behind the test-only switch `WABOROS_JIT_TWOLEVEL=1`.

Results:
* **CoreMark code byte-identical on both architectures in both bounds modes.** With the switch on
  and off, `tools/jit_dump.py` lists of all 5 979 modules (spec cache and CoreMark) differ in 4
  modules in each of the four cells: `memory64/address64.0-3`.
* Identical bytes give identical speed, so no timed run was needed on either architecture. D35
  already shows that cross-compiled aarch64 bytes equal the native runner's.
* Spec suite on `jit-x86_64`: 63 231 pass, 0 fail, 93 xfail in both modes. Memory subset on
  `jit-aarch64` under QEMU: 10 988 pass, 0 fail, 2 xfail in both modes. `tests/jit`: 337 passed, as
  with the switch off.
* Compile time +3.9 % (explicit) and +3.2 % (guard), paired medians on `jit-x86_64` (BENCHMARKS.md,
  "JIT low layer").
* The edge tests caught one trap: a generic op lost an immediate range that the specialised code had
  carried in its branch condition (LESSONS 83).

The maintainer accepted the identity as the speed evidence, accepted the four `address64`
differences, set a compile-time bound of +5 %, and asked for this spec, with the bulk-memory, table
and element checks in its scope (2026-11-04).

## Goals / Non-goals
* Goals:
  * The bounds checks of the group are written once, in `jit/common/`, over the low layer.
    Afterwards no backend contains wasm bounds semantics for these operators: which width, the
    wrap before the offset, the constant fold, guard coverage, the i64 `lo` fast path, the carry,
    "ends past 2⁶⁴", the trap code.
  * The low layer is a stated contract: operations, operand kinds, flags, scratch registers,
    immediate ranges per encoding, and the fault-point rule. GC and a third architecture build on it.
  * Generated code stays byte-identical, except for the modules listed in "Identity". The speed
    evidence is identity, as in the reconnaissance.
  * Compile time grows by at most +5 %.
* Non-goals:
  * Control-flow semantics: calls, `call_indirect` beyond its element check, tail calls, traps and
    unwinding, `br_table`.
  * Exceptions (they may be written in parallel, per backend, as today).
  * Arithmetic, floats, conversions, SIMD lanes.
  * The copy and fill loops of bulk memory and tables. They stay per backend; only their end checks
    move.
  * The artifact format and the enumerated LIR vocabulary of ROADMAP "Next" item 6.
  * An sljit backend.
  * Any change of the D39 interface.

**Relation to the LIR vocabulary (ROADMAP item 6).** D39's methods are the LIR (D26): the operations
the shared core calls in one pass. The low layer sits **below** them, inside the backend side of the
D39 boundary. It is never serialized and never appears in the artifact. When item 6 enumerates the
LIR operation names, the `ll_*` names and their condition names (D39's `gt_u`, `lt_u`, `ge_u`) go
into the same list as a second, lower section. That list is the one a C or Zig micro-compiler ports
as the per-architecture interface. Each `ll_*` op maps to one to three sljit calls (SLJIT-BACKEND.md):
`ADD`, `ADD` with `SET_CARRY` plus a jump on carry, a compare and jump, and a memory operand. On
sljit, `ll_fold` would always add the offset into the index, because sljit's two-register memory
operand has no displacement.

## Design

### Where the code lives, the plug points and the switch
* **Shared layer:** `src/waboros/targets/jit/common/twolevel.py` during steps 1-3, renamed at step 4
  (Q1). It has no encoder imports and names no machine register. It calls only the low layer;
  the plug point creates the trap stub (`_stub(f, code)`) and passes its label, so the shared
  layer never sees a frame.
* **Low layer:** methods `ll_*` and the attributes `IDX`, `TMP` on each `Backend` class
  (`x86_64/backend.py`, `aarch64/backend.py`). They are private to the JIT and are not part of D39.
* **Plug points:** the private methods `_bounds`, `_end_check` and `_element`. Both backends have
  them with the same role. While the switch is on, each one's first statement delegates to the
  shared layer and returns the backend's own access descriptor: `Mem` on x86-64,
  `(base, index | None, offset)` on aarch64, and for `_element` the registers today's callers read.
  `load`, `store`, `simd`, `memory_fill`, `memory_copy`, the table operators and `call_indirect`
  are not edited. They already reach the checks through these three methods.
* **Switch:** `WABOROS_JIT_TWOLEVEL=1`, read in `Backend.__init__`, test-only, off by default (as
  `WABOROS_JIT_NOPIN`). It stays until step 4, so the old and the new code can be compared byte for
  byte and mixed for bisection (CLAUDE.md). Step 4 removes the switch and the old bodies.
* **From step 4 (amendment B2, below):** the module is `common/lower.py` (Q1). The plug points
  are `_bounds` and `_element`, now one delegation each, and the four D39 methods `memory_copy`,
  `memory_fill`, `table_copy`, `table_fill`. Each of these four creates its stub, calls the
  shared function of the same name, then emits its own copy or fill loop on the registers
  `ll_three` returned. `_end_check`, `_three`, `_bounds64` and `_guarded` are gone.
  `memory.init` and `table.init` have no JIT end check: they run through the host bridge.

### The shared semantics (normative)
`bounds(b, f, addr, offset, size, mem, a64, lo)`, for an access of `size` bytes at `addr + offset`:
1. `base, limit = ll_mem(mem)`; `oob = _stub(f, MEMORY_OUT_OF_BOUNDS)`, created by the plug point
   `_bounds`, which first asserts that no fault point is pending ("Fault points").
2. **An i64 index in a register or slot** (`a64` and not `Imm`): `r = ll_index64(addr)`.
   * If `offset + size > 2⁶⁴ − 1`, then `ll_jump(oob)` and `offset = 0` (LESSONS 75: no index is in
     bounds; the dead code that follows stays well formed).
   * `k = offset + size`. If `k ≤ lo`: `ll_jcmp(gt_u, r, ll_add(TMP, limit, −k), oob)`. Here `lo` is
     the declared minimum, so `limit − k` cannot wrap.
   * Otherwise: `ll_jcmp(gt_u, ll_add_jc(TMP, r, k, oob), limit, oob)`.
   * Then `r, disp = ll_fold(r, offset, size)` and return `ll_access(base, r, disp, size)`.
   * This is explicit in every mode: no guard covers a 64-bit index (S0009).
3. `unchecked = guard and not a64 and covered`. "Covered" means `(addr mod 2³²) + offset + size ≤
   2³² + GUARD` for a constant, or `offset + size ≤ GUARD` otherwise (S0006). If unchecked:
   `ll_fault_next(oob)`.
4. **A constant address** (i32, or i64 `Imm`): `ea = (addr mod 2³² or 2⁶⁴) + offset`.
   * If `ea + size > 2⁶⁴ − 1`: `ll_jump(oob)`, `ea = 0`.
   * If checked: `ll_jcmp_imm(lt_u, limit, ea + size, oob)`.
   * Return `ll_access(base, None, ea, size)`.
5. **An i32 index:** `r = ll_index32(addr)`. This is a 32-bit move, or one 32-bit add for `AddK`: the
   wasm wrap happens before the offset is added (LESSONS 5).
   * `r, disp = ll_fold(r, offset, size)`.
   * If checked: `ll_jcmp(gt_u, ll_add(TMP, r, disp + size), limit, oob)`. All values are below 2³³,
     so nothing wraps in 64 bits.
   * Return `ll_access(base, r, disp, size)`.

`end_check(b, oob, r, n, limit, wide)` (bulk memory and tables): trap to `oob` unless `r + n ≤ limit`,
where `n` is the count register of the operation. When not `wide`, both operands are zero-extended
i32 values (each below 2³², the sum below 2³³), so the sum cannot wrap. When `wide`, a carry out of
64 bits traps. `r` is preserved. The width of each operand (`n` is wide only between two wide
ranges) and the order (both ranges of a copy are checked before any write) stay in the per-backend
callers until step 4 (Q2).

**Bulk memory and tables, from step 4 (amendment B2).** `end_check` is replaced by four functions
named after the D39 methods that call them. Each is a single `ll_check_sum` per range. `oob` is the
plug point's stub: `MEMORY_OUT_OF_BOUNDS` for the memory operators, `TABLE_OUT_OF_BOUNDS` for the
table ones. A table's elements' base and size are context words `w` and `w + 1`.
* `memory_copy(b, oob, d, s, n, dmem, smem, (ad, as))`:
  `rd, rs, rn = ll_three(d, s, n, ad, as, ad and as)`; then for `(rd, dmem, ad)` and then
  `(rs, smem, as)`: `base, limit = ll_mem(mem)`, `ll_check_sum(r, rn, limit, oob, w)`,
  `ll_addr(r, base)`.
* `memory_fill(b, oob, d, v, n, mem, a64)`: `rd, _, rn = ll_three(d, v, n, a64, False, a64)`;
  `base, limit = ll_mem(mem)`; `ll_check_sum(rd, rn, limit, oob, a64)`; `ll_addr(rd, base)`.
* `table_fill(b, oob, w, i, v, n, a64)`: `ri, _, rn = ll_three(i, v, n, a64, True, a64)`;
  `ll_check_sum(ri, rn, ll_ctx_word(w + 1), oob, a64)`; `ll_elem_addr(ri, w)`.
* `table_copy(b, oob, wd, ws, d, s, n, (ad, as))`: `rd, rs, rn = ll_three(d, s, n, ad, as, ad and
  as)`. Then `ll_check_sum(r, rn, ll_ctx_word(w + 1), oob, wide)` for the destination, then the
  source. Then `ll_elem_addr(rd, wd)`, `ll_elem_addr(rs, ws)`.

The rules these carry:
* Every operand is read at its width before anything is written: `ll_three`, LESSONS 32. `n` is
  64-bit only between two 64-bit ranges. `memory.fill`'s value is an i32 and `table.fill`'s a
  64-bit reference.
* Every range is checked before the backend's loop writes anything (no partial fill or copy).
* A copy checks the destination first, then the source. Both have the same trap code, so the
  order is not observable.
* The order of checks and address forms is aarch64's code before step 4, per operator:
  * `memory_copy` checks a range, then forms its address, before the other range. A memory other
    than 0 holds its base and size in `x11`/`x12`, which the other memory needs next.
  * `table_copy` checks both ranges, then forms both addresses.
  * A single order for both operators would change 106 aarch64 modules (every `table.copy`), so
    it is not used. A unification can come with a later spec that changes code anyway.

The shared functions emit no write to linear memory or a table. The loop each D39 method emits
afterwards stays per backend (Non-goals).

`element(b, oob, word, index, a64)`: the index goes into `IDX` (`ll_index`) as a 64-bit value
(`a64`) or zero-extended, copied even from a register, and is compared with the table's size word
(`word + 1`): `ge_u` traps `TABLE_OUT_OF_BOUNDS`. The result is the element: 8 bytes at the
elements' base (context word `word`) plus 8 · `IDX`.

### The low-layer contract
Operand kinds:
* **wasm operands:** D39's `Imm`, `Reg`, `Slot`, `AddK`.
* **register ids:** `int`.
* **handles:** `base`, `limit`, a table word. These are opaque values a backend returns and accepts
  back: x86-64 returns a register or a `Mem` on the context, aarch64 returns a register it has
  loaded.
* **labels:** the backend's `Label`.
* **conditions:** D39's names, unsigned 64-bit.

General rules, for every op:
* **Registers.** An op writes only the registers its row lists: its scratch registers, its
  `dst`, and `IDX`/`TMP` where stated. It never writes a pool register, the frame, context or
  memory-0 registers. Two kinds of op may write registers that hold the operands of the calling
  D39 method:
  * **The bulk ops**, `ll_three`, `ll_addr`, `ll_elem_addr` and `ll_check_sum`. They write the
    bulk operand registers (x86-64 `rax`, which is `tos`, plus `rdx` and `rcx`; aarch64
    `x13`-`x15`). D39 allows that in table and bulk operations. `ll_three` reads every operand
    before it writes a register that may hold one (LESSONS 32). x86-64 `ll_check_sum` writes its
    `r` and restores it on the fall-through.
  * **Every other op** writes no such register and not `tos`.

  A store's source is formed after `_bounds`, as today.
* **Flags (LESSONS 1).** Only `ll_jcmp`, `ll_jcmp_imm`, `ll_add_jc` and `ll_check_sum` produce
  flags that are consumed. Each consumes them inside itself, in the instruction after the one that
  set them. Other ops may clobber flags (x86 `add`, `sub`, `shl`); nothing may rely on flags
  across ops.
* **Immediates.** Every op accepts the full value range stated for it and fits the value to the
  encoding itself. **The ranges below are part of the contract** (LESSONS 83). An op that cannot
  encode a value raises; it never masks (LESSONS 75).
* **Linear memory.** The only instruction that touches linear memory is the access the caller
  emits after `ll_access` returns, or the loop a bulk D39 method emits after its shared function
  returns. Ops may read context words and frame slots before it, which never fault.

| op | meaning | x86-64 | aarch64 |
|---|---|---|---|
| `IDX`, `TMP` | scratch registers for the index and the check | `rcx`, `rdx` | `x9`, `x10` (also `x16`; `x11`/`x12` for a memory other than 0; `x17` for a context word past 4095, and for a frame slot past the scaled imm12 range: slot 2048 for a 4-byte load, 4096 for an 8-byte one) |
| `ll_mem(mem)` → `(base, limit)` | the memory's base and size in bytes | memory 0: `r13`, `r14`; other: `Mem(r15, 8·mem)`, `Mem(r15, 8·mem+8)`; emits nothing | memory 0: `x26`, `x25`; other: `ldp x11, x12` (or two `ldr`; past context word 4095 through `x17`) |
| `ll_index32(a)` → `IDX` | `IDX = a mod 2³²`, zero-extended | `Reg`: `mov ecx, r32`; `Slot`: `mov ecx, [r12+8k]`; `Imm`: `mov ecx, imm32`; `AddK`: `lea ecx, [id+k]` (k as a signed disp32) | `Reg`: `mov w9, wN`; `Slot`: `ldr w9, [x27, #8k]` for slot k < 2048, else `mov x17, 8k; ldr w9, [x27, x17]`; `Imm`: `movz`/`movk w9`; `AddK`: `add w9, wid, #k` or `sub w9, wid, #(−k mod 2³²)` (imm12, optionally `lsl #12`), else `mov w16, k; add w9, wid, w16` |
| `ll_index64(a)` → reg | the 64-bit index in a register | `Reg` itself, or `mov rcx, [slot]` | `Reg` itself, or `ldr x9, [slot]` |
| `ll_fold(r, off, size)` → `(r', disp)` | the address `r + off` in the access form the architecture has | `(r, off)` if `off + size < 2³¹`; else `mov rdx, off; add rcx, rdx` (`r` = `rcx`) or `mov rcx, off; add rcx, r`, giving `(rcx, 0)` | `(r, 0)` if `off = 0`; else `x9 = r + off` (imm12 or `lsl #12`, or via `x16`), giving `(x9, 0)` |
| `ll_add(dst, src, k)` → `dst` | `dst = src + k` mod 2⁶⁴; `src` a register or `limit` | register `src`: `lea` if −2³¹ ≤ k < 2³¹; else `mov dst, imm64; add dst, src` (`dst` ≠ `src`) or `mov rdx, imm64; add dst, rdx`. `Mem` `src`: `mov dst, src; add/sub dst, abs(k)` **only if −2³¹ < k < 2³¹** (`sub` takes `abs(k)` as a sign-extended imm32); else `mov dst, imm64; add dst, src` | `add`/`sub` imm12 (`lsl #12`), else `mov x16, k; add` |
| `ll_add_jc(dst, src, k, L)` → `dst` | `dst = src + k`; branch to `L` on a carry out of 64 bits; 0 ≤ k < 2⁶⁴ | `mov dst, src; add dst, imm32` if k < 2³¹, else `mov dst, imm64; add dst, src`; then `jb L` | `adds dst, src, #imm12`, else via `x16`; `b.hs L` |
| `ll_jcmp(c, x, y, L)` | branch if `x c y`; `y` a register or `limit` | `cmp x, y; j<c>` | `cmp x, y; b.<c>` |
| `ll_jcmp_imm(c, x, v, L)` | branch if `x c v`, 0 ≤ v < 2⁶⁴ | `cmp x, imm32` if v < 2³¹, else `mov rdx, imm64; cmp x, rdx`; `j<c>` | `subs xzr, x, #imm12`, else `mov x10, v; cmp`; `b.<c>` |
| `ll_jump(L)` | unconditional | `jmp` | `b` |
| `ll_access(base, r, disp, size)` → descriptor | the access `[base + r + disp]` | `r` none: `[base + disp]` if disp < 2³¹, else `mov rcx, imm64` and indexed; register base: `[base + r + disp]`; `Mem` base: `add rcx, base` (`r` = `rcx`) or `mov rcx, base` | `r` none: `[base, #disp]` if `disp % size = 0` and `disp / size < 4096`, else `mov x9, disp`; disp ≠ 0 with `r`: `x9 = r + disp`; then `[base, r]` |
| `ll_fault_next(L)` | the next memory instruction is a guard fault point resuming at `L` (S0006) | sets the pending fault point | the same |
| `ll_check_sum(r, n, limit, L, wide)` (step 2; Q2) | branch to `L` unless `r + n ≤ limit`; with `wide`, also on a carry out of 64 bits; `r` preserved; not `wide` requires `r + n < 2⁶⁴`; no immediate | `add r, n; [jb L;] cmp r, limit; ja L; sub r, n`: writes only `r` (`rax`/`rdx`, restored on the fall-through); `n` = `rcx`; `limit` `r14` or a context word | `add[s] x9, r, n; [b.hs L;] cmp x9, limit; b.hi L`: writes `x9`; `r` = `x13`/`x14`, `n` = `x15`; `limit` `x25`, `x12` or `x10` |
| `ll_index(a, wide)` → `IDX` (step 2) | `IDX = a`, 64-bit (`wide`) or the i32 zero-extended; any `Imm`, `Reg`, `Slot`, copied even from a register (LESSONS 32) | `wide`: `mov rcx, …` (64-bit); else as `ll_index32` | `mov x9/w9, …`, `ldr`, or `mov_imm` |
| `ll_ctx_word(w)` → handle (step 2) | context word `w` (a table's size word) | `Mem(r15, 8w)`, emits nothing (disp32) | `ldr x10, [ctx, #8w]` for w ≤ 4095, else `mov x17, 8w; ldr x10, [ctx, x17]` |
| `ll_element(w, IDX)` → element (step 2) | 8 bytes at `[context word w] + 8·IDX` | `mov rdx, [r15+8w]`, giving `[rdx + rcx·8]` | `ldr x10, …` as `ll_ctx_word`, giving `(x10, x9)` read as `[x10, x9, lsl 3]` |
| `ll_three(x, y, z, wx, wy, wz)` → `(rx, ry, rz)` (step 4) | the three operands of a bulk operator, each 64-bit if its `w`, else the i32 zero-extended (a 32-bit move even from a register, LESSONS 5); any `Imm`, `Reg`, `Slot`; every operand read before a register that may hold one is written (LESSONS 32); no flags | `rdx = y`, `rcx = z` first, `rax = x` last (`rax` is `tos` and may hold an operand; `rdx`, `rcx` never do): `mov r, …` (64-bit) or `mov r32, …` (i32), a constant by the encoder's `mov` immediate form; returns `(rax, rdx, rcx)`; writes only those | `x13 = x`, `x14 = y`, `x15 = z` in that order (none holds an operand): `mov x/w, …`, `ldr` (`x17` past the scaled imm12 range, as `ll_index32`), `movz`/`movk`; returns `(13, 14, 15)`; writes those and `x17` |
| `ll_addr(r, base)` (step 4) | `r = base + r`: the address of a checked memory range; `base` from `ll_mem`; no immediate | `add r, r13` or `add r, [r15+8·mem]` (disp32); writes `r`, clobbers flags | `add r, r, x26` or `add r, r, x11`; writes `r`, no flags |
| `ll_elem_addr(r, w)` (step 4) | `r = [context word w] + 8·r`: the address of a checked table range | `shl r, 3; add r, [r15+8w]` (disp32); writes `r`, clobbers flags | `ldr x10, …` as `ll_ctx_word` (`x17` past word 4095); `add r, x10, r, lsl #3`; writes `r`, `x10` (`x17`), no flags |

Every row was built and checked: steps 1 and 2 in the package. The step-4 rows were checked in
step 4 in the package: aarch64 byte-identical, x86-64 only the listed reorder.

### Fault points (S0006, D45)
Unchanged. `ll_fault_next` records the pending point. The caller (`load`, `store`, `simd`) calls
`_fault_point()` right before its one memory instruction, and no push is outstanding there. The
plug point `_bounds` of each backend asserts that no point is pending before it calls `bounds`.
The shared layer cannot see the pending point, and the assert stays there after step 4.

### Rows (D47)
None. The code is per architecture; nothing differs by operating system.

## Identity (acceptance of every step)
`tools/jit_dump.py --target {jit-x86_64,jit-aarch64}` in `WABOROS_JIT_BOUNDS=guard` and `=explicit`,
switch on against switch off. The lists must be equal except for these listed modules, each with its
reason:
* `memory64/address64/address64.0-3.wasm`, on both targets and in both modes. These have i64
  accesses with offsets beyond 2³¹, on a memory whose declared minimum is 1 page (so `k > lo`,
  the carry path):
  * x86-64: an offset with `offset + size ≥ 2³¹` is added into the index by `ll_fold`:
    `mov rcx, off; add rcx, r`, or `mov rdx, off; add rcx, rdx` when the index was loaded from
    its slot into rcx. Before, it was `lea rcx, [rdx − size]` from the checked end, for
    offset ≥ 2³¹.
  * aarch64: on the carry path the index with an offset is `add x9, r, #off` instead of
    `sub x9, x10, #size`.
  * The same semantics and the same trap behaviour (spec suite and `test_memory64.py`). Accepted by
    the maintainer (2026-11-04).

Not in the spec cache or CoreMark, so the D35 lists do not show them; each is correct (step 1
review, 2026-11-05) and accepted:
* **x86-64, an i32 or i64 constant address whose end `ea + size` is at or above 2³¹, wherever it is
  checked.** That is explicit mode, plus in guard mode an i64 constant (always checked) or an i32
  constant past the reservation of 4 GiB + GUARD.
  * Before, the address was loaded first (`mov rcx, ea; lea rdx, [rcx + size]; cmp rdx, limit;
    ja`). The shared layer checks first (`mov rdx, ea + size; cmp limit, rdx; jb`), then forms the
    address (`mov rcx, ea`).
  * For `ea ≥ 2³¹` this is the same 4 instructions and 1 byte more. For `2³¹ − size ≤ ea < 2³¹`
    it is one instruction fewer, since the access then takes `ea` as its displacement.
  * Such an address is in bounds only for a memory of 2 GiB or more:
    `test_two_gib_memories` reads it back there, on memories 0 and 1 in both modes, and the
    reviewer checked in- and at-bound accesses from `0x7FFFFFF8` to `0x8000FFF9`.
  * Accepted by the maintainer (2026-11-05), on the reviewer's confirmation. Removing it would need
    a fused constant-address check op on every architecture, which moves semantics down, or a
    predicate the shared layer asks.
* **x86-64, an i64 offset with `2³¹ − size ≤ offset < 2³¹`.** Before, it stayed the disp32 of the
  access. `ll_fold` keeps a displacement only while `offset + size < 2³¹`, so it is now added into
  the index (`mov ecx, off; add rcx, r`).
* **x86-64, an i64 access with `2³¹ ≤ k ≤ lo`** (a declared minimum of 2 GiB or more).
  * Before, it took the carry path: the x86-64 fast path needed `k < 2³¹`.
  * The shared layer takes the `k ≤ lo` path. For `k = 2³¹` on memory 0 that is
    `lea rdx, [r14 − 0x80000000]`; otherwise `ll_add` legalizes `limit − k` (`mov rdx, −k;
    add rdx, limit`).
  * aarch64 already used `k ≤ lo` and does not change.
* Step 2 adds no expected difference. A difference it shows is either removed or listed here, with
  its reason and the maintainer's acceptance.

**CoreMark (`poc/in/core.wasm`) must be byte-identical on both targets in both modes**. That is the
speed evidence; a CoreMark difference needs a placement-controlled measurement (S0007 method) before
the step can close. The test modules of `tests/jit` that are not in the spec cache (for example
`test_k_at_two_to_the_31_on_a_memory_other_than_0`) may differ in the same way as `address64`. They
are checked by their tests, not by identity.

### Step 4: the D35 lists before and after the switch-over (amendment B2)
The comparison is made once, since the switch is gone afterwards. "Before" is the switch-on code at
the step's base commit and "after" is the step-4 code, both targets, both modes, 5 979 modules. The
switch-on lists at the base already differ from the switch-off ones in `address64.0-3` only, as
above. Checked at step 4, in the package:
* **aarch64: identical**, guard and explicit.
* **x86-64: exactly the 70 modules that contain `memory.copy`**, in both modes:
  * `bulk-memory/bulk/bulk.3`;
  * `bulk-memory/memory_copy/memory_copy.1`-`.18` and `.83`-`.96`;
  * `bulk-memory/memory_init/memory_init.3`;
  * `memory64/bulk64/bulk64.2`;
  * `memory64/memory_copy64/memory_copy64.1`-`.18` and `.83`-`.96`;
  * `memory64/memory_init64/memory_init64.3`;
  * `multi-memory/memory_copy0/memory_copy0.0` and `multi-memory/memory_copy1/memory_copy1.0`.

  The reason: `memory_copy` takes aarch64's order, and the destination's `add rax, base` moves from
  after the source check to before it. In every one of the 70 the code has the same length and the
  same multiset of instructions (branch targets aside): a reorder, no extra instruction.
  `memory.fill`, `table.fill` and `table.copy` are unchanged on x86-64.
* **CoreMark: identical** on both targets in both modes.
* **The generated modules of `tests/jit/test_low_layer.py`:** the same result. Only the bulk module
  differs, on x86-64 only, pinned and with `WABOROS_JIT_NOPIN=all`. This includes a module of i64
  memories with a declared minimum of 0 pages and accesses at offset 0: the carry path without an
  offset. It is identical before and after, and also with the switch off at the base. That closes
  the step-3 review's point that every `I64_SAME` case had `k ≤ lo`.

## Test plan
* **Identity test** (new, `tests/jit/test_low_layer.py`): compile CoreMark and generated modules
  covering every path of the check (every width; i32 register, `local + const` and constant
  addresses; memory 0 and a memory read from its context words; the i64 paths that stay identical)
  with the switch on and off, for both targets (cross-compiled, D35) and both
  modes. Assert that the code is equal, except for the listed modules. It runs in the default suite
  until step 4. At step 4 the D35 lists before and after the removal are compared once instead.
* **Every step:**
  * `WABOROS_JIT_TWOLEVEL=1 uv run python -m tests.spec.run --engine jit-x86_64`, guard and
    explicit: 0 fail, the xfails of today.
  * The same for `jit-aarch64` under QEMU (`tools/run_arm64.py … --in-process`), guard and explicit.
  * `WABOROS_JIT_TWOLEVEL=1 uv run pytest tests/jit -q` in both modes: as today.
  * `tools/isa_all.py` for both targets: clean.
  * CI (`ci.yml`, step 1 to step 3): the `spec` job (x86-64) and the `arm64` job (native
    aarch64) gain one step with the switch on (spec suite, both modes).
* **Edge tests** with the switch on. Existing: `test_memory64.py`, including
  `test_k_at_two_to_the_31_on_a_memory_other_than_0`, `test_every_width_at_each_bound_and_offset`
  and the 2⁶⁴ − 1 offset; `test_tables.py`; the table64 tests. New: one test per immediate-range
  boundary in the contract table, using the value at the boundary and one past it: `k = 2³¹` on
  both `ll_add` forms, `k = 2³¹ − 1`, imm12 4095, 4096 and 4097, `lsl #12`, and a constant address
  `ea = 2³¹ − size` and `2³¹`.
* **Mutations** (LESSONS 80), in a subprocess, with a crash counted as "caught". Each must be caught
  by the tests:
  * drop the i32 compare;
  * drop the i64 carry branch;
  * use the `lo` fast path for `k > lo`;
  * drop "ends past 2⁶⁴";
  * drop the wide carry in `ll_check_sum`;
  * use `gt_u` for `ge_u` in `element`;
  * read an i32 table index without zero-extending it (LESSONS 5).
  The script is a scratch tool, reported in the step, not committed.
* **Step 4 (amendment B2):**
  * `test_switch_changes_no_code` is deleted, and the comparison above replaces it.
    `test_boundaries_*` and `test_end_and_element_checks_*` compare with `pyc` instead of the
    switch off, as the rest of `tests/jit` does. The trap assertions stay as they are.
    `test_an_i32_address_ignores_the_upper_half` compares the dirtied build with the clean one
    once, not once per switch value. `test_two_gib_memories` keeps its exact expectations, with
    no second build.
  * `_bulk_text` gives the memories sizes of 1 and 2 pages (each i32 memory pairs with an i64
    memory of the other size) and the tables 3, 4 and 5 elements. A range checked against the
    other memory's or table's size then fails.
  * `test_an_i32_address_ignores_the_upper_half` dirties the i32 operands of `memory.copy`,
    `memory.fill`, `table.copy` and `table.fill`, including copies between an i64 and an i32
    memory or table.
  * The mutation list above is run again, and so are these, each caught:
    * no source check, or no destination check, in `memory_copy`;
    * the source checked after the copy loop (x86-64);
    * an address formed before its check;
    * `n` always narrow, or wide when either side is (`memory_copy`, `table_copy`);
    * `d` and `s` widths swapped;
    * each range given the other range's carry flag (`memory_copy`, `table_copy`);
    * `memory_fill` `d` or `n` narrow on an i64 memory, or no carry;
    * `table_fill` value read as an i32, index narrow on a table64, or no check;
    * no source check in `table_copy`;
    * a range checked against the other table's or memory's size;
    * x86-64 `ll_three` writing `rax` first (LESSONS 32);
    * `ll_three` not zero-extending an i32 register (both architectures);
    * aarch64 `ll_elem_addr` with `lsl #2`, `ll_addr` adding `xzr`, and `ll_check_sum` without
      the wide carry, under QEMU.

    `memory_fill`'s value read as 64 bits survives: it is an equivalent mutant, since both loops
    keep only the low byte.
* **Compile time:** CoreMark plus the three corpus modules, median of 10 compiles, at least 10
  alternated pairs, switch on against off, both targets, both modes. Bound: **+5 %** (paired
  median). Recorded in BENCHMARKS.md. At step 4: the step against the base commit with the switch
  off (the bound's reference) and on, plus the worst case of step 2 (a module of 300 functions of
  bulk and table operators).

## Plan of steps
JIT, ABI and trap work: `jit-engineer` with a plan approved first, then a `reviewer` (CLAUDE.md).
Measurements run with no other agent benchmarking at the same time.

| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | The memory bounds in the package behind the switch: `common/twolevel.py` with `bounds`; the low-layer ops of the contract table (the rows built in the prototype) in both backends; the `_bounds` hooks; the identity test; the boundary tests; CI steps | jit-engineer | Identity (only `address64.0-3`; CoreMark equal); Test plan "every step"; mutations caught; compile time ≤ +5 % | src ~260, tests ~80, CI ~10 |
| 2 | `_end_check` and `_element` through the shared layer: `ll_check_sum`, `ll_index`, `ll_ctx_word`, `ll_element` (Q2); hooks in both backends | jit-engineer | Identity with no new listed module; bulk, table and table64 tests and the spec suite with the switch; mutations; compile time | src ~90, tests ~120 |
| 3 | Review of steps 1-2 against this contract, without the implementers' reasoning: the R3 check, flags, registers, immediate ranges, fault points | reviewer | findings closed | — (done 2026-11-05; findings closed in this text and in step 4, Changes) |
| 4 | Switch-over (amendment B2): delete the old `_bounds`, `_bounds64`, `_guarded` bodies, `_end_check`, `_three` and the per-backend `_element` body, and the switch and its CI steps. The bulk callers' rules (operand widths, the order, both ranges before any write) move into the shared layer as `memory_copy`, `memory_fill`, `table_fill`, `table_copy` over the new ops `ll_three`, `ll_addr`, `ll_elem_addr`. Rename the module (Q1). The identity test becomes the one-time D35 comparison of "Identity", step 4 | jit-engineer | "Identity", step 4: aarch64 identical; x86-64 exactly the 70 listed modules, each a reorder; CoreMark identical. Spec suite on `jit-x86_64` both modes and on `jit-aarch64` (QEMU subset, CI native) as before; `tests/jit` as before minus the identity test; ISA gates clean; the step-4 mutations caught; compile time within +5 % of the switch-off base; a short `memory.copy` micro-benchmark, alternated, shows no change | src −380 +165 (prototype: −378 +162); tests −110 +120; CI −8; docs ~+120 |

A step that needs more than its size stops and reports. **Docs per step** (STANDARDS.md):
* step 1: BENCHMARKS.md (compile time); LESSONS.md for a trap; this spec's Changes;
* step 2: the same;
* step 4:
  * ARCHITECTURE.md: the file list, where `twolevel.py` becomes `lower.py` and holds steps 1, 2
    and 4; "The IR": the backend side of D39 is two levels, shared semantics over a low layer.
  * DECISIONS.md: a new decision (unify over a low layer; the contract is this spec).
  * ROADMAP.md: the status line and "Next" item (3b).
  * TESTING.md: the CI description loses the switch steps.
  * BENCHMARKS.md: compile time and the `memory.copy` micro-benchmark.
  * LESSONS.md: test operands of equal size hide which size was checked.
  * This spec `implemented`; `docs/specs/README.md`.

## Alternatives considered
* **Keep the semantics per backend (today).** No work now. But exceptions and GC would be written
  twice, and a third architecture would write them a third time. The bounds check, the one place
  memory safety rests on (R3), would stay in two copies that must be reviewed separately. Rejected by
  the maintainer after the reconnaissance.
* **A thinner, sljit-like vocabulary without `ll_fold`.** The offset always goes into the index, so
  the shared layer needs no address-mode op. Prototyped as `WABOROS_JIT_TWOLEVEL=thin`:
  * aarch64 code unchanged;
  * x86-64 changes 17 modules including CoreMark, which grows by 1 128 bytes in guard mode and 828 in
    explicit mode (one `lea` per access with an offset);
  * spec parity held.
  Its speed was **not measured** (maintainer, 2026-11-04). The thin vocabulary changes CoreMark, so
  it would need a placement-controlled run. The `ll_fold` vocabulary gives identity at no extra cost
  in lines. The sljit PoC's ~100 % with this shape was measured without placement control and is
  not evidence either way.
* **A materialised low-level IR with its own selector.** Rejected by D26: compile time, with nothing
  measured that it would buy.
* **Putting the semantics into the D39 core (`common/compiler.py`) and narrowing D39.** This would
  change the frozen interface and both backends' `supported` contract for no gain over a layer
  beside the core. Kept as the direction for a later spec only if the low layer grows past this
  group.

## Open questions
* **Q1 The module's final name. Decided 2026-11-05 (maintainer): as recommended.** Recommendation: `jit/common/lower.py` at step 4, since it will hold
  GC semantics too. `twolevel.py` names the experiment.
* **Q2 The end check as one fused op. Decided 2026-11-05 (maintainer): as recommended.** x86-64 has no free register in `memory_copy` (`rax`, `rdx`,
  `rcx` hold operands), so it adds in place and subtracts afterwards. Composing `ll_add` and
  `ll_jcmp` would need a scratch that does not exist there. Recommendation: one fused
  `ll_check_sum(r, n, limit, L, wide)`. Alternative: an `ll_add_inplace` with an `ll_undo`, which
  is two ops for one meaning. Amended at step 2 (maintainer, 2026-11-05): the shared `end_check`
  is the one `ll_check_sum` call; which operands are wide, the trap code and the order (both
  ranges before any write) stay in the per-backend callers, which steps 1-3 do not edit. They
  move into the shared layer at step 4, when the callers do (`lower.py`). Amended for step 4
  (option B2, maintainer, 2026-11-05):
  * the operand widths and the order move into `lower.py`, as "Bulk memory and tables";
  * the order is aarch64's per operator, so aarch64 stays byte-identical and x86-64
    `memory_copy` changes by a reorder only;
  * new ops: `ll_three`, `ll_addr`, `ll_elem_addr`;
  * the trap code stays where step 1 put it: the plug point creates the stub and passes its label
    (Q5).
* **Q5 The trap code of the bulk operators (step 4).** The amended Q2 lists "the trap code" among
  what moves into the shared layer. But since step 1 the plug point creates the stub, so that the
  shared layer never sees a frame. Two options:
  * (a) keep that: each D39 bulk method writes `_stub(f, TrapCode.…)`, one token per method,
    caught by the tests that assert the code;
  * (b) the shared functions take a `stub(code) -> label` callable and name the code. That is a
    closure per operator and, for consistency, a change of `bounds` and `element` on the hot path
    as well.

  Recommendation: (a). **Decided 2026-11-05 (maintainer): (a).**
* **Q3 The switch after step 4. Decided 2026-11-05 (maintainer): as recommended.** Recommendation: remove it. Unlike `WABOROS_PYC_BR`, the old code
  is deleted, so there is nothing to mix, and the D35 lists keep the comparison.
* **Q4 The `address64` differences.** Accepted (maintainer, 2026-11-04). Removing them would take an
  "end register" hint in `ll_fold` (~6 lines per architecture). Not planned.

## Changes
* 2026-11-04: created (draft), from the reconnaissance of ROADMAP "Next" 3b.
* 2026-11-05: approved by the maintainer with the recommendations (Q1-Q3).
* 2026-11-05: step 1 implemented: `common/twolevel.py` and the low layer of both backends behind
  `WABOROS_JIT_TWOLEVEL=1`, with the `_bounds` hooks, `tests/jit/test_low_layer.py` and the CI
  steps. D35 lists: the switch off equals the previous code on both targets in both modes; the
  switch on differs only in `address64.0-3`, and CoreMark is identical. Compile time is within
  the bound (BENCHMARKS.md, "JIT low layer"). Mutations of the check are caught. Changes against
  the text above:
  * the plug point passes the trap label to the shared layer;
  * `ll_mem` is the backend's `_mem` itself, and x86-64's `ll_index32` emits the move directly:
    one Python frame less each, which took x86-64 explicit compile time from +5.7 % to +1.1 %;
  * the identity test uses generated modules, not `test_memory64.py`'s;
  * the x86-64 constant-address difference in "Identity" was found, and awaits acceptance.
* 2026-11-05: step 1 review: no defect. "Identity" now describes the constant-address difference
  precisely (not only in explicit mode; the same instruction count, or one fewer) and lists two
  more x86-64 i64 classes. The maintainer accepted all three on the reviewer's confirmation. New
  tests:
  * `test_two_gib_memories`: in-bounds and at-bound accesses past 2³¹ on memories of 32769 pages,
    and the i64 `k ≤ lo` path at k = 2³¹;
  * `test_an_i32_address_ignores_the_upper_half`: LESSONS 5; it catches the zero-extension
    mutation;
  * i64 constants from 2³¹ to 2⁶⁴ − 8 in the boundary test.

  The CI step is in the `spec` job.
* 2026-11-05: step 2 implemented: `end_check` and `element` in `common/twolevel.py`, the hooks
  in `_end_check` and `_element` of both backends, and the ops `ll_check_sum`, `ll_index`,
  `ll_ctx_word` and `ll_element`. The step-2 rows of the contract table are now confirmed. D35
  lists: switch off equals the previous code, switch on equals the previous switch-on lists, on
  both targets in both modes; no new listed module. Compile time is within the bound
  (BENCHMARKS.md, "S0011 step 2 in the package"). The mutations of the test plan are caught, on
  aarch64 under QEMU too. Changes against the text above (maintainer, 2026-11-05):
  * G1: `ll_index` is a new op. `ll_index64` returns a register operand in place, but the element
    check needs the index copied into `IDX`, since `table_set` then writes `rax` (LESSONS 32).
  * G2: the aarch64 scratch registers include `x17` for a context word past 4095. That applies to
    `ll_ctx_word` and `ll_element`, and to step 1's `ll_mem` for a memory whose words are there.
  * G3: Q2's wording is amended: the shared `end_check` is one `ll_check_sum` call. The operand
    widths and the order of the checks move into the shared layer at step 4, with their callers.
  * G4: the signatures are `end_check(b, oob, r, n, limit, wide)` and
    `element(b, oob, word, index, a64)`: the label is passed as in step 1, and `n` is explicit.
  * G5: a narrow sum is below 2³³, not 2³⁵.
  * G6: `LowLayer` has a fourth type variable, for the element `ll_element` returns. On aarch64
    that is `(x10, x9)` with the scale, unlike `ll_access`'s triple.
  * G7: one more mutation, an i32 table index read without zero-extension. It is caught by
    `test_an_i32_address_ignores_the_upper_half`, which now also covers `table.get`, `table.set`
    and `call_indirect`.
  * New tests: the identity test covers bulk memory and the table operators (pinned and with
    locals in slots) and 2 060 tables whose words cross context word 4096.
    `test_end_and_element_checks_match_the_old_code` runs fills, copies and element accesses at
    the bound, one past it and where the end wraps 2⁶⁴, switch on against off.
* 2026-11-05: step 3, the review of steps 1-2. Findings closed here, or in step 4:
  * The identity test's `I64_SAME` claimed the carry path, but every case had `k ≤ lo`. A module
    of i64 memories of minimum 0 pages, with accesses at offset 0, was compared once: switch on
    equals switch off on both targets in both modes. The test goes at step 4, and the one-time
    comparison includes that module ("Identity", step 4).
  * The "no pending fault point" assert lives in the plug points `_bounds`, not the shared layer.
    "Fault points" now says so. It stays there after step 4.
  * The general rules contradicted the table: the flags rule now lists `ll_check_sum`; the
    register rule names the bulk ops and `ll_check_sum`'s restored `r`; the aarch64 `ll_index32`
    row gives the `sub` form and the slot load through `x17` past slot 2048.
* 2026-11-05: amendment B2 approved by the maintainer with the step-4 plan:
  * the bulk and table callers' rules move into `lower.py` ("Bulk memory and tables");
  * three new ops in the table, marked step 4;
  * aarch64's order per operator, not one order for both: one order would have changed 106
    aarch64 modules;
  * Q5 (a): the trap code stays at the plug point;
  * a permanent test of the i64 carry path without an offset.

  The step-3 review's first finding was B2 itself.
* 2026-11-05: step 4 implemented; S0011 implemented (D53).
  * `common/twolevel.py` is now `common/lower.py`, with `memory_copy`, `memory_fill`,
    `table_fill` and `table_copy` over the new ops `ll_three`, `ll_addr` and `ll_elem_addr`.
  * Removed: the old `_bounds`, `_bounds64` and `_guarded` bodies, `_end_check`, `_three`, the
    per-backend `_element` body, the switch, and its two CI steps.
  * D35 lists against the base's switch-on lists, 5 979 modules: aarch64 identical in both
    modes; x86-64 exactly the 70 modules of "Identity", step 4, in both modes, each the same
    length and instructions reordered; CoreMark identical.
  * Spec suite: `jit-x86_64` 63 354 pass, 0 fail, 78 xfail in both modes, as at the base.
    `jit-aarch64` under QEMU, on the bulk-memory, memory64, multi-memory, table, memory,
    `call_indirect` and `address` files: 17 928 pass, 0 fail, 4 xfail in both modes, as at the
    base.
  * `uv run pytest -q`: 674 passed, 72 skipped. `tests/jit` loses the 4 identity cases. The
    low-layer, table, memory64 and memories tests under QEMU: 85 passed, 3 skipped, in both
    modes.
  * ISA gates: 0 failed on both targets.
  * Mutations: the 36 of the Test plan are caught (28 natively, 8 under QEMU). The equivalent
    mutant (`memory_fill`'s value read as 64 bits) survives, as stated.
  * Compile time against the switch-off base: −0.4 % to +1.6 % on the standard set, and +1.9 %
    on the bulk worst case (x86-64, 20 pairs). A short `memory.copy` runs at 0.99× (BENCHMARKS.md,
    "S0011 step 4").
  * New test: i64 memory `$e` of minimum 0 pages in `test_boundaries_match_pyc`, the carry path
    without an offset, grown to 1 page and loaded at the bound.
