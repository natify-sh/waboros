---
title: JIT low layer - wasm memory semantics written once over per-architecture operations
kind: spec
id: S0011
status: draft
superseded_by:
related: [D24, D26, D35, D39, D44, D45, D47, R3, S0004, S0006, S0007, S0009]
owner: maintainer
updated: 2026-11-04
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
  (Q1). It has no encoder imports and names no machine register. It calls only the low layer and
  the backend's existing `_stub(f, code)`.
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

### The shared semantics (normative)
`bounds(b, f, addr, offset, size, mem, a64, lo)`, for an access of `size` bytes at `addr + offset`:
1. `base, limit = ll_mem(mem)`; `oob = _stub(f, MEMORY_OUT_OF_BOUNDS)`. Assert that no fault point
   is pending.
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

`end_check(b, f, r, limit, code, wide)` (bulk memory and tables): trap `code` unless `r + n ≤ limit`,
where `n` is the count register of the operation. Both operands are below 2³⁵ when not `wide`, so the
sum cannot wrap. When `wide`, a carry out of 64 bits traps. Both ranges of a copy are checked before
any write; that order stays in the per-backend callers. `r` is preserved.

`element(b, f, word, index, a64)`: the index goes into `IDX` as a 64-bit value (`a64`) or
zero-extended, and is compared with the table's size word: `ge_u` traps `TABLE_OUT_OF_BOUNDS`. The
result is the element's address, scaled by 8.

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
* **Registers.** An op writes only the scratch registers listed below, its `dst`, and `IDX`/`TMP`
  where stated. It never writes a pool register, `tos`, the frame, context or memory-0 registers,
  or the operand registers of the calling D39 method (for example x86-64 `rax`, `rdx`, `rcx` in
  `memory_copy`). A store's source is formed after `_bounds`, as today.
* **Flags (LESSONS 1).** Only `ll_jcmp`, `ll_jcmp_imm` and `ll_add_jc` produce flags that are
  consumed, and each consumes them in its own next instruction. Other ops may clobber flags (x86
  `add`, `sub`); nothing may rely on flags across ops.
* **Immediates.** Every op accepts the full value range stated for it and fits the value to the
  encoding itself. **The ranges below are part of the contract** (LESSONS 83). An op that cannot
  encode a value raises; it never masks (LESSONS 75).
* **Linear memory.** The only instruction that touches linear memory is the access the caller
  emits after `ll_access` returns. Ops may read context words before it, which never fault.

| op | meaning | x86-64 | aarch64 |
|---|---|---|---|
| `IDX`, `TMP` | scratch registers for the index and the check | `rcx`, `rdx` | `x9`, `x10` (also `x16`; `x11`/`x12` for a memory other than 0) |
| `ll_mem(mem)` → `(base, limit)` | the memory's base and size in bytes | memory 0: `r13`, `r14`; other: `Mem(r15, 8·mem)`, `Mem(r15, 8·mem+8)`; emits nothing | memory 0: `x26`, `x25`; other: `ldp x11, x12` (or two `ldr`) |
| `ll_index32(a)` → `IDX` | `IDX = a mod 2³²`, zero-extended | `mov ecx, …` / `lea ecx, [id+k]` | `mov w9, …` / `add w9, wid, #k` (imm12, or via `x16`) |
| `ll_index64(a)` → reg | the 64-bit index in a register | `Reg` itself, or `mov rcx, [slot]` | `Reg` itself, or `ldr x9, [slot]` |
| `ll_fold(r, off, size)` → `(r', disp)` | the address `r + off` in the access form the architecture has | `(r, off)` if `off + size < 2³¹`; else `mov rdx, off; add rcx, rdx` (`r` = `rcx`) or `mov rcx, off; add rcx, r`, giving `(rcx, 0)` | `(r, 0)` if `off = 0`; else `x9 = r + off` (imm12 or `lsl #12`, or via `x16`), giving `(x9, 0)` |
| `ll_add(dst, src, k)` → `dst` | `dst = src + k` mod 2⁶⁴; `src` a register or `limit` | register `src`: `lea` if −2³¹ ≤ k < 2³¹; else `mov dst, imm64; add dst, src` (`dst` ≠ `src`) or `mov rdx, imm64; add dst, rdx`. `Mem` `src`: `mov dst, src; add/sub dst, abs(k)` **only if −2³¹ < k < 2³¹** (`sub` takes `abs(k)` as a sign-extended imm32); else `mov dst, imm64; add dst, src` | `add`/`sub` imm12 (`lsl #12`), else `mov x16, k; add` |
| `ll_add_jc(dst, src, k, L)` → `dst` | `dst = src + k`; branch to `L` on a carry out of 64 bits; 0 ≤ k < 2⁶⁴ | `mov dst, src; add dst, imm32` if k < 2³¹, else `mov dst, imm64; add dst, src`; then `jb L` | `adds dst, src, #imm12`, else via `x16`; `b.hs L` |
| `ll_jcmp(c, x, y, L)` | branch if `x c y`; `y` a register or `limit` | `cmp x, y; j<c>` | `cmp x, y; b.<c>` |
| `ll_jcmp_imm(c, x, v, L)` | branch if `x c v`, 0 ≤ v < 2⁶⁴ | `cmp x, imm32` if v < 2³¹, else `mov rdx, imm64; cmp x, rdx`; `j<c>` | `subs xzr, x, #imm12`, else `mov x10, v; cmp`; `b.<c>` |
| `ll_jump(L)` | unconditional | `jmp` | `b` |
| `ll_access(base, r, disp, size)` → descriptor | the access `[base + r + disp]` | `r` none: `[base + disp]` if disp < 2³¹, else `mov rcx, imm64` and indexed; register base: `[base + r + disp]`; `Mem` base: `add rcx, base` (`r` = `rcx`) or `mov rcx, base` | `r` none: `[base, #disp]` if `disp % size = 0` and `disp / size < 4096`, else `mov x9, disp`; disp ≠ 0 with `r`: `x9 = r + disp`; then `[base, r]` |
| `ll_fault_next(L)` | the next memory instruction is a guard fault point resuming at `L` (S0006) | sets the pending fault point | the same |
| `ll_check_sum(r, n, limit, L, wide)` (step 2; Q2) | trap unless `r + n ≤ limit`; with `wide`, a carry traps; `r` preserved | `add r, n; [jb L;] cmp r, limit; ja L; sub r, n` | `add[s] x9, r, n; [b.hs L;] cmp x9, limit; b.hi L` |
| `ll_ctx_word(w)` → handle, `ll_element(w, IDX)` → descriptor (step 2) | a table's size word, and the element address `base + 8·IDX` | `Mem(r15, 8w)`; `mov rdx, [r15+8w]`, giving `[rdx + rcx·8]` | `ldr x10, [ctx, #8w]`; `ldr x10, [ctx, #8w]`, giving `(x10, x9, lsl 3)` |

The two step-2 rows are a design to be confirmed by step 2 (Q2). Their shapes are today's code.
The rows above them were built and checked in the prototype.

### Fault points (S0006, D45)
Unchanged. `ll_fault_next` records the pending point. The caller (`load`, `store`, `simd`) calls
`_fault_point()` right before its one memory instruction, and no push is outstanding there. The
shared layer asserts that no point is pending when it starts.

### Rows (D47)
None. The code is per architecture; nothing differs by operating system.

## Identity (acceptance of every step)
`tools/jit_dump.py --target {jit-x86_64,jit-aarch64}` in `WABOROS_JIT_BOUNDS=guard` and `=explicit`,
switch on against switch off. The lists must be equal except for these listed modules, each with its
reason:
* `memory64/address64/address64.0-3.wasm`, on both targets and in both modes. These have i64
  accesses with offsets beyond 2³¹, on a memory whose declared minimum is 1 page:
  * x86-64: an offset ≥ 2³¹ − size is formed by `mov rcx, imm64; add rcx, r` instead of
    `lea rcx, [rdx − size]`.
  * aarch64: on the carry path the index with an offset is `add x9, r, #off` instead of
    `sub x9, x10, #size`.
  * The same semantics and the same trap behaviour (spec suite and `test_memory64.py`). Accepted by
    the maintainer (2026-11-04).
* Step 2 adds no expected difference. A difference it shows is either removed or listed here, with
  its reason and the maintainer's acceptance.

**CoreMark (`poc/in/core.wasm`) must be byte-identical on both targets in both modes**. That is the
speed evidence; a CoreMark difference needs a placement-controlled measurement (S0007 method) before
the step can close. The test modules of `tests/jit` that are not in the spec cache (for example
`test_k_at_two_to_the_31_on_a_memory_other_than_0`) may differ in the same way as `address64`. They
are checked by their tests, not by identity.

## Test plan
* **Identity test** (new, `tests/jit/test_low_layer.py`): compile CoreMark and the modules of
  `test_memory64.py` with the switch on and off, for both targets (cross-compiled, D35) and both
  modes. Assert that the code is equal, except for the listed modules. It runs in the default suite
  until step 4. At step 4 the D35 lists before and after the removal are compared once instead.
* **Every step:**
  * `WABOROS_JIT_TWOLEVEL=1 uv run python -m tests.spec.run --engine jit-x86_64`, guard and
    explicit: 0 fail, the xfails of today.
  * The same for `jit-aarch64` under QEMU (`tools/run_arm64.py … --in-process`), guard and explicit.
  * `WABOROS_JIT_TWOLEVEL=1 uv run pytest tests/jit -q` in both modes: as today.
  * `tools/isa_all.py` for both targets: clean.
  * CI (`ci.yml`, step 1 to step 3): the `jit` and `arm64` jobs gain one step with the switch on
    (spec suite, both modes).
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
  * use `gt_u` for `ge_u` in `element`.
  The script is a scratch tool, reported in the step, not committed.
* **Compile time:** CoreMark plus the three corpus modules, median of 10 compiles, at least 10
  alternated pairs, switch on against off, both targets, both modes. Bound: **+5 %** (paired
  median). Recorded in BENCHMARKS.md.

## Plan of steps
JIT, ABI and trap work: `jit-engineer` with a plan approved first, then a `reviewer` (CLAUDE.md).
Measurements run with no other agent benchmarking at the same time.

| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | The memory bounds in the package behind the switch: `common/twolevel.py` with `bounds`; the low-layer ops of the contract table (the rows built in the prototype) in both backends; the `_bounds` hooks; the identity test; the boundary tests; CI steps | jit-engineer | Identity (only `address64.0-3`; CoreMark equal); Test plan "every step"; mutations caught; compile time ≤ +5 % | src ~260, tests ~80, CI ~10 |
| 2 | `_end_check` and `_element` through the shared layer: `ll_check_sum`, `ll_ctx_word`, `ll_element` (Q2); hooks in both backends | jit-engineer | Identity with no new listed module; bulk, table and table64 tests and the spec suite with the switch; mutations; compile time | src ~90, tests ~30 |
| 3 | Review of steps 1-2 against this contract, without the implementers' reasoning: the R3 check, flags, registers, immediate ranges, fault points | reviewer | findings closed | — |
| 4 | Switch-over: delete the old `_bounds`, `_bounds64`, `_guarded`, the per-backend `_end_check` and `_element` bodies and the switch; rename the module (Q1); the identity test becomes a one-time D35 comparison of the lists before and after | jit-engineer | D35 lists after the removal equal the switch-on lists; spec, tests and ISA gates as today; compile time re-measured | src about −250 +20 |

A step that needs more than its size stops and reports. **Docs per step** (STANDARDS.md):
* step 1: BENCHMARKS.md (compile time); LESSONS.md for a trap; this spec's Changes;
* step 2: the same;
* step 4: ARCHITECTURE.md "The IR" (the backend side of D39 is two levels: shared semantics over a
  low layer); DECISIONS.md, a new decision (unify over a low layer; the contract is this spec);
  ROADMAP.md; this spec `implemented`; `docs/specs/README.md`.

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
* **Q1 The module's final name.** Recommendation: `jit/common/lower.py` at step 4, since it will hold
  GC semantics too. `twolevel.py` names the experiment.
* **Q2 The end check as one fused op.** x86-64 has no free register in `memory_copy` (`rax`, `rdx`,
  `rcx` hold operands), so it adds in place and subtracts afterwards. Composing `ll_add` and
  `ll_jcmp` would need a scratch that does not exist there. Recommendation: one fused
  `ll_check_sum(r, n, limit, L, wide)`, with the wasm rules (which operands are wide, when a carry
  traps, which trap code, both ranges before any write) in the shared layer. Alternative: an
  `ll_add_inplace` with an `ll_undo`, which is two ops for one meaning.
* **Q3 The switch after step 4.** Recommendation: remove it. Unlike `WABOROS_PYC_BR`, the old code
  is deleted, so there is nothing to mix, and the D35 lists keep the comparison.
* **Q4 The `address64` differences.** Accepted (maintainer, 2026-11-04). Removing them would take an
  "end register" hint in `ll_fold` (~6 lines per architecture). Not planned.

## Changes
* 2026-11-04: created (draft), from the reconnaissance of ROADMAP "Next" 3b.
