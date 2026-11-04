---
title: memory64 and table64 (Wasm 3.0) on pyc, jit-x86_64 and jit-aarch64
kind: spec
id: S0009
status: approved
superseded_by:
related: [D23, D24, D25, D27, D29, D35, D36, D37, D39, D44, D45, D47, D48, S0001, S0003, S0006, S0007, R3, R6]
owner: maintainer
updated: 2026-11-04
---

# S0009 memory64 and table64 (Wasm 3.0) on pyc, jit-x86_64 and jit-aarch64

## Summary
Wasm 3.0 gives every memory and every table an **address type**, `i32` or `i64`. An `i64` memory
takes `i64` addresses and has up to 2⁴⁸ pages; an `i64` table takes `i64` indices. Wasmtime 50
enables memory64 by default ([WASM-PROPOSALS.md](../WASM-PROPOSALS.md)). This is the next phase-5
parity step (ROADMAP "Next"; exceptions and GC follow). The spec fixes the front-end encoding, the
`pyc` helpers, the JIT bounds checks for 64-bit addresses, one amendment of the backend interface
(D39), the size limits, and the steps.

Main choices (decided by the maintainer on 2026-11-04 unless marked open, "Open questions"):
* The front end records the address type in two new lists. Instruction tuples and limits tuples
  stay as they are, so i32 code on every target is unchanged **by construction**.
* `pyc` needs only typing and four small helpers. Its addresses are already Python ints, and the
  errors an out-of-range address raises are already mapped to traps on 64- and 32-bit hosts.
* The JIT uses **explicit checks only** for i64 memories, in both bounds modes, written the way
  Wasmtime writes them: 3 instructions per access in the common case. The guard-page trick cannot
  cover a 64-bit index (see "Why guard pages cannot cover an i64 index"). The Wasmtime-style hybrid
  (guard region plus one compare) is optional step 5, taken only after a measurement (Q2).
* **Memory limits are Wasmtime's** (Q1 (b)). No 65 536-page cap for i64 memories: the JIT reserves
  as Wasmtime 50.0.1 does (measured, "Runtime") and grows an i64 memory past its reservation by
  **moving** it; `pyc` grows its `bytearray` until allocation fails. An i32 memory never moves, and
  its code bytes do not change (D35). Tables keep the D27 cap.
* **Context ABI v1 is amended, not bumped** (Q4): no new word. The libcalls `memory.grow` and
  `table.grow` return −1 at the address width, and the base of an i64 memory may change in a host
  call.

**Status before this step** (measured 2026-11-04, `uv run python -m tests.spec.run --engine pyc
memory64`; `jit-x86_64` gives the same numbers): 8 719 commands, **6 739 pass / 1 921 fail / 59
skip**. Grouped by cause:

| cause | commands | what |
|---|---|---|
| decoding | 231 | root failures: `NotImplementedError: limits flags 4` (106) or `5` (125) in `binary.py` `limits()` (203 `module`, 26 `assert_unlinkable`, 2 `module_definition`) |
| GC types | 1 (+1) | `table_init64.wast:2457` declares an `array` type (`rec / sub / struct / array types`); its `assert_return` fails with it. These wait for GC |
| cascade | 1 689 | commands of a module that did not load: 1 398 "no current module", 254 "no exported function" (an earlier module is still current), 37 results of that stale module (27 "no trap", 5 wrong values, 3 table and 2 indirect-call traps) |
| validation | 0 failing, **286 passing for the wrong reason** | 285 of the 374 `assert_invalid` and 1 `assert_malformed` are rejected by `NotImplementedError` (kind `None`), which `run.py` counts as a pass (Q7). Only 89 are rejected by `Invalid` |
| execution | 0 | nothing runs |
| text-only | 59 skip | `assert_malformed` in text format |

Oracle: `--engine wasmtime memory64` gives 8 658 pass / 0 fail / 2 xfail / 59 skip. The two xfails
are `table64.wast:13` (our `spectest` has no `table64`, Q5) and `table_grow64.wast:46` (Wasmtime
reports out-of-memory instead of returning −1, "Limits").

## Goals / Non-goals
* Goals: `memory64/` passes on `pyc`, `jit-x86_64` and `jit-aarch64` (guard and explicit, aarch64
  under QEMU) and each of them claims it (D25 parity: `spec_diff` against `pyc` is empty). The only
  expected failures are the 2 GC commands, plus `table64.wast:13` until Q5 is done. For i32-only
  modules, the generated `pyc` source and the D35 lists of both JITs (guard and explicit, CoreMark
  included) stay byte-identical. Baseline ISA only (D24). No per-OS code (D47: no rows touched).
* Non-goals: guard pages for i64 memories (the hybrid is optional, Q2); moving an i32 memory; an
  OS-specific move (`mremap`, D47); shared memories and limits flags 2, 3, 6 and 7 (threads; they
  stay `NotImplementedError`; a shared memory never moves); custom page sizes; `memory.discard`;
  `mpy`; the public API (`Config.memory_reservation`, store limits, `data_ptr`: phase 6); a speed
  claim (none is made).

## Interface

### Front end (`frontend/binary.py`, `frontend/validate.py`, `frontend/simd.py`)
* **`R.limits()`** accepts flags `0x04` (i64, min) and `0x05` (i64, min, max), for memories and
  tables (Wasm 3.0 binary format, "Limits": `0x04 n:u64`, `0x05 n:u64 m:u64`). It returns
  `(min, max, at)`. Its callers store `(min, max)` exactly as today and append `at` (`0x7F` i32,
  `0x7E` i64) to two **new module lists**: `mod["maddr"]` (one entry per memory) and
  `mod["taddr"]` (one per table), imports first, in the order of `mems` and `ttypes`. Flags 2, 3, 6
  and 7 stay `NotImplementedError` (shared), and flags ≥ 8 stay `Malformed`. Nothing else in the
  parser changes: memarg offsets are already read as u64, and constant expressions already carry
  i64 values.
* **No instruction tuple changes** (D37's contract: the memory index stays last). A consumer looks
  up the address type by the memory or table index.
* **Validator** (Wasm 3.0, "Instructions"; `at` is the address type of the memory or table named,
  `at'` = i32 if either of two address types is i32):

| what | rule |
|---|---|
| limits | memory: `at` i32 ≤ 65 536 pages, i64 ≤ 2⁴⁸; table: i32 ≤ 2³²−1, i64 ≤ 2⁶⁴−1; min ≤ max |
| memarg | offset < 2³² for an i32 memory, < 2⁶⁴ for an i64 memory (the reader already bounds it) |
| scalar and SIMD loads/stores, `load_lane`/`store_lane` | the address operand is `at` (`OPSIG` keeps i32; memory ops get their own branch; `simd.validate` takes the address type from `need_mem`, which returns it) |
| `memory.size` / `memory.grow` | `[] → [at]` / `[at] → [at]` |
| `memory.fill` / `memory.copy x y` / `memory.init` | `[at i32 at]` / `[at_x at_y at']` / `[at i32 i32]` |
| `table.get` / `table.set` / `table.size` / `table.grow` | `[at] → [t]` / `[at t]` / `[] → [at]` / `[t at] → [at]` |
| `table.fill` / `table.copy x y` / `table.init` | `[at t at]` / `[at_x at_y at']` / `[at i32 i32]` |
| `call_indirect`, `return_call_indirect` | the callee index is `at` of the table |
| active data / element segment | the offset expression has type `at` of its memory / table |

* **Linking** (`runtime/link.py`, shared): an imported memory or table must have the **same**
  address type as the export (`memory64-imports.wast`); limits are matched as today.
* **JIT until it is ready** (`common/compiler.py` `unsupported`): a module with an i64 memory
  reports `"memory64"`, one with an i64 table reports `"table64"`. Steps 2a and 3 remove these.

### `pyc` (`targets/pyc/codegen.py`, `runtime.py`, `__init__.py`)
**Nothing changes for an i32 memory or table.** Every new branch tests `maddr`/`taddr`, and the i64
paths use new helper names, so the source of an i32-only module stays byte-identical (the Test
plan checks its sha256).
* **Loads and stores: no change.** The address `a+offset` is already a Python int of any size. An
  address past the end raises `IndexError` or `struct.error`. One of 2⁶³ or more raises
  `OverflowError` (`unpack_from`, CPython 3.13, checked 2026-11-04) or `IndexError` ("cannot fit
  'int' into an index-sized integer", for `M[a]`, `pack_into`, item assignment). `_run` already maps
  all three to `MEMORY_OUT_OF_BOUNDS`. A 32-bit host raises `OverflowError` from 2³¹ on, as it does
  for i32 today ([PYC-PLATFORMS.md](../PYC-PLATFORMS.md)). The SIMD helpers `_rd`/`_wr` compare `a + n`
  with `len(M)` first.
* `memory.size`: the same text `(len(M)>>16)`, typed `I64` with bound 2⁴⁸. `table.size` likewise.
* `memory.grow` on an i64 memory: `_grow64(M, maxpages, n)` returns `0xFFFFFFFFFFFFFFFF` when
  `old + n > maxpages`, and also when `bytes(n << 16)`/`extend` raises `MemoryError` or
  `OverflowError`. `maxpages` is `min(max, 2⁴⁸ − 1)`: the byte size of an i64 memory must fit 64
  bits, as in Wasmtime ("Limits"); there is **no other cap**. `table.grow`: `_tgrow64`, the same cap
  of 10 000 000 elements as D27, −1 as `M64`.
* Active segments: `_dseg64`/`_eseg64` take the 64-bit offset without the `& 0xFFFFFFFF` of
  `_dseg`/`_eseg`. `memory.copy`, `memory.fill`, `memory.init` and the table helpers compare with
  Python ints and need no change.
* Memory creation: the generated line for an i64 memory checks before it allocates. A min of 2⁴⁸
  pages or more raises `MemoryError` with Wasmtime's message (Q6, "Limits"); a `MemoryError` or
  `OverflowError` from the allocation itself becomes `MemoryError("failed to allocate N bytes for
  a memory of P pages")`, both **at instantiation**. Module definitions do not allocate, so
  `memory64.wast:8` (a definition with min 2⁴⁸) passes.
* **`pyc` memory is resident**: `bytearray(n)` writes its zeros and `extend` copies them (the
  JIT commits lazily). On the dev VM (CPython 3.13.17, 15 GiB, no swap, `overcommit_memory` 0,
  2026-11-04) `bytearray(2³⁶)`, `bytearray(2⁴⁸)` and `extend(bytes(2³⁶))` raise `MemoryError`. So
  pyc follows Wasmtime's rule with the host's RAM as the bound: a 64 GiB min fails there where
  Wasmtime instantiates. A grow the kernel admits but cannot back ends in the OOM killer, as for any
  Python program; no spec command comes near it. On a 32-bit host, `bytearray` past 2³¹ raises
  `OverflowError` or `MemoryError`: −1.
* A live `memoryview` of the memory blocks `extend` (`BufferError`, ARCHITECTURE.md). That is the
  existing host rule for i32 memories and is not caught.
* **Speed of the i32 path: zero change**, since its source is identical. No measurement is needed
  (as in S0008).

### JIT: why guard pages cannot cover an i64 index
For an i32 memory, the index is below 2³² and the static offset is below 2³². Every effective
address is therefore below 2³³, and the largest offsets are checked statically anyway. One
reservation of 4 GiB + 32 MiB that is never readable past the memory's size contains every address
an unchecked access can form, so the fault handler can turn every out-of-bounds access into a trap
(S0006). An i64 index goes up to 2⁶⁴−1. User space has 2⁴⁷ bytes of address space on x86-64 with
4-level paging (2⁵⁶ with LA57) and 2⁴⁸ on a typical aarch64 kernel. No reservation can contain
`base + index`, so the address could land in unrelated mapped memory of the process instead of
faulting. The index must be **compared at run time**, in every mode. The only question is how much
of the check the guard can still absorb: the static offset (the hybrid below), not the index.

### JIT: bounds checks for an i64 memory (explicit, both modes)
Let `k = offset + size` (static), `L` = the memory's size in bytes (r14 / x25 for memory 0, the
context word for the others), and `min` = the module's declared minimum of that memory in bytes
(for an import, the import's declared minimum; a memory never shrinks, so `L ≥ min` always).
Wasmtime 50.0.1 with `signals_based_traps = False` emits this same shape (disassembled 2026-11-04 from
`Module.serialize()`: `mov rax,[bound]; sub rax,0xc; cmp rdx,rax; ja trap` for offset 8;
`mov; add 0x10000004; jb trap; cmp; ja trap` for offset 2²⁸):
* **`k ≤ min`** (the usual case: small offsets, memories of at least one page): `L − k` cannot
  wrap. x86-64: `lea rdx, [r14 − k]; cmp idx, rdx; ja oob`; aarch64: `sub x10, x25, #k` (or `mov` +
  `sub`), `cmp x9, x10; b.hi oob`. The index is used as is when it is already in a register (a
  pinned local or `tos`); otherwise it is loaded by a **64-bit** move.
* **Otherwise**: `idx + k` is computed in 64 bits with the carry tested. x86-64: `mov rdx, idx;
  add rdx, k` (or `mov rdx, imm64; add rdx, idx`) `; jc oob; cmp rdx, L; ja oob`; aarch64: `adds
  x10, x9, k; b.cs oob; cmp x10, L; b.hi oob`. Flags are consumed by the next instruction (LESSONS
  1). The `k ≤ min` path uses **only the module's declared minimum, never the runtime's size**, so
  an artifact stays correct for a host with a larger limit (D36); on x86-64 it also needs
  `k < 2³¹` (the sign-extended imm32 of `lea`/`sub`). When `offset + size ≥ 2⁶⁴` no index is in bounds: the backend
  emits `jmp oob` (a 64-bit `k` cannot even be formed; aarch64's `mov` would wrap it silently). The
  shortcut for `k > max·65536` of the draft was dropped (2a: an optimisation without a measurement).
* **Constant address** (`Imm`): `ea = value + offset` without the `& M32` of the i32 path (that mask
  would be a silent wrong-address bug). `ea + size > 2⁶⁴` gives `jmp oob`; otherwise it compares with
  `L` as the i32 path does (`cmp L, imm` when below 2³¹, else through `rdx`/`x10`).
* The access: `[r13 + idx + offset]` (x86-64; `offset` in the displacement when it is below 2³¹,
  otherwise `lea rcx, [rdx − size]` from the checked end: `rdx` stays free for the stored value),
  `[x26, x9]` after adding `offset` (aarch64; `sub x9, x10, #size` on the carry path).
* **`AddK` (S0007 K) does not apply**: an `ADDK` entry is an i32 value (`i32.add` of a pinned i32),
  and validation never lets it address an i64 memory; the backend asserts this. A 64-bit form
  (`lea` wraps mod 2⁶⁴, as `i64.add` does) would be correct but is an optimisation without a
  measurement: not now.
* **Bulk memory** with an i64 memory: the operands are read by 64-bit moves (`_three` gains the
  64-bit case), and the range check `d + n ≤ L` uses `add; jc oob; cmp; ja oob`, because `d + n` can
  wrap in 64 bits (`_end_check` assumes both values are below 2³²). `memory.copy` between memories
  of different types checks each range at its own width, as the validator types it.
* **Instruction count** (memory 0, index in a register; aarch64 adds `add x9, x9, #offset` when
  `offset ≠ 0`):

| access | x86-64 | aarch64 |
|---|---|---|
| i32, explicit (today) | `mov ecx`; `lea`; `cmp`; `ja` (4) | `mov w9`; `add`; `cmp`; `b.hi` (4) |
| i32, guard (today) | `mov ecx` (1) | `mov w9` (1) |
| i64, `k ≤ min` | `lea`; `cmp`; `ja` (3) | `sub`; `cmp`; `b.hi` (3) |
| i64, otherwise | `mov`; `add`; `jc`; `cmp`; `ja` (5) | `adds`; `b.cs`; `cmp`; `b.hi` (4) |
| i64, hybrid (option, Q2) | `cmp`; `ja` (2) | `cmp`; `b.hi` (2) |

**Multi-memory with mixed types (D37)**: the bounds mode belongs to the module (D29), but the check
is chosen per access. In a guard-mode module, accesses to i32 memories are unchecked fault points as
today, and accesses to i64 memories take the explicit path above and are not fault points. The
fault map lists i32 accesses only. A guard module registers the reservations of its i32 memories
as today (`instance.py:605-608`); its i64 memories are not registered ("Runtime"). Nothing changes
in the handler or its rows (D47).

### JIT option: the hybrid check (Wasmtime-style; Q2)
On a verified guard row only, for an i64 memory with `k ≤ GUARD` (32 MiB): `cmp idx, L; ja oob`,
then the access `[base + idx + offset]` as a **fault point**. If `idx ≤ L`, the access ends before
`base + L + GUARD`. The bytes `[base + L, base + L + GUARD)` are never accessible: pages past the
size are uncommitted, and the reservation keeps a GUARD tail behind the largest size. So an access
past the end faults, and the handler resumes at the `MEMORY_OUT_OF_BOUNDS` stub, as in S0006.
Larger offsets keep the full check, with the 64-bit overflow trap. Wasmtime 50.0.1 does exactly
this with signals on (disassembled 2026-11-04: `lea r8,[base+idx+8]; cmp idx, bound; cmova r8, 0;
mov eax,[r8]`, that is, a branch-free Spectre mask to address 0 instead of `ja`; for offset 2²⁸:
`add idx, 0x10000004; jb trap; cmp; cmova`).
* **Benefit**: per access it saves 1 instruction against the `k ≤ min` path (the `lea`) and 3
  against the general path. It does not save the compare and branch that the i32 guard mode
  removes. Not measured: the only CoreMark build is wasm32. A wasm64 build is possible on the dev
  machine (`clang` and `wasm-ld` are installed) but was not tried for this draft.
* **Cost**: fault points and fault-map entries for i64 accesses, a second code path in `_bounds`
  on both backends, and a host obligation for guard artifacts with an i64 memory ("GUARD
  inaccessible bytes behind `size_bytes`, at every size"). Our reservation already meets that
  obligation: every i64 reservation keeps a GUARD tail behind its capacity ("Runtime"). The hybrid
  also needs i64 reservations **in the registry**, which this spec leaves out ("Runtime"). A move
  would then register the new reservation (`guard.add_reservation`) before it publishes the new
  base. The old entry goes through the old reservation's `on_free` hook (`guard.py:561`,
  `_forget` at `:564`) before the unmap. The code itself is unaffected, because it rereads the
  base after every call (below).
* **Decided (Q2, 2026-11-04)**: explicit only in this spec. The hybrid is an optional later step,
  taken only if a wasm64 CoreMark run on a guard row shows a gain worth the second path.

### Backend interface: D39 amendment B (both backends, one step, reviewer; Q3 (b))
Every backend method whose operands address a memory or a table gains a **required keyword-only**
parameter that gives the address type, with no default, so a call site that forgets it fails at
once (`TypeError`) instead of compiling i32 checks for an i64 memory:
```python
def load(self, f, size, signed, wide, dst, addr, offset, mem, *, a64: bool, lo: int) -> None: ...
def store(self, f, size, addr, offset, src, mem, *, a64: bool, lo: int) -> None: ...
def simd(self, f, op, dst, args, imm, *, a64: bool, lo: int) -> None: ...  # False, 0: no memory
def memory_fill(self, f, d, v, n, mem, *, a64: bool) -> None: ...
def memory_copy(self, f, d, s, n, dmem, smem, *, a64: tuple[bool, bool]) -> None: ...  # (dst, src)
def table_get(self, f, dst, word, index, *, a64: bool) -> None: ...
def table_set(self, f, word, index, value, *, a64: bool) -> None: ...
def table_fill(self, f, word, i, v, n, *, a64: bool) -> None: ...
def table_copy(self, f, dst, src, d, s, n, *, a64: tuple[bool, bool]) -> None: ...  # (dst, src)
def call_indirect(self, f, table, sig, index, shift_slots, *, a64: bool) -> None: ...
def tail_indirect(self, f, table, sig, index, src, n, *, a64: bool) -> None: ...
```
`lo` (also required, step 2a): the memory's declared minimum in bytes if it is i64 (an import's
own declared minimum), else 0; the `k ≤ min` path needs it, and it is a lower bound of the size at
run time (linking guarantees it; `JitInstance` checks it again, `LinkError`). The core passes the
value from `maddr`/`taddr` at every call. **Contract**: with `a64` true (for a
copy: the side's flag), the address or index operand is a full 64-bit value; a copy's `n` is 64-bit
only when both flags are true (the narrower type, as the validator). With `a64` false, operands
are i32 and the method emits exactly today's code. The core never passes an `AddK` with `a64` true.
`memory_size` and the table-size `ctx_load` are unchanged (both words are already 64-bit). Under
D39 this is an amendment (signatures and operand meaning change), so: both backends in the same
step, x86-64 byte identity for every module without an i64 memory or table (D35 lists), and a
`reviewer` pass. The sljit PoC backend (`poc/sljit/sljit-be`) is outside the package and follows
when it is next run.

**Table64 on the JIT**: `_element` loads the index with a 64-bit move and compares it with the
64-bit `size` word (`cmp; jae`, no carry possible). `table_fill`/`table_copy` use the carry-checked
end check. `call_indirect` and `tail_indirect` take the table record through `_element`. The
`table.grow` and `table.init` libcalls receive 64-bit slots already.

### Runtime (`jit/common/instance.py`, `execmem.py`) and the context ABI
**An i32 memory does not change**: 4 GiB + GUARD reserved, committed on grow, never moved, at most
65 536 pages. Its `Memory` takes the same path as today, and no code byte changes.

**Reservation of an i64 memory** (Q1 (b)). The sizes are Wasmtime 50.0.1's on a 64-bit Linux host,
measured 2026-11-04 from `/proc/self/maps` around `Memory.data_ptr` (wasmtime-py, default `Config`).
The measured values are those of `memory_reservation` = 4 GiB, `memory_reservation_for_growth` =
2 GiB and `memory_guard_size` = 32 MiB:

| min bytes `m` | Wasmtime 50.0.1 (measured) | ours |
|---|---|---|
| `m ≤ 4 GiB` (mins 1, 49 152, 65 535, 65 536 pages) | 4 GiB + 32 MiB after the base | `RESERVATION64 + TAIL64`: the same |
| `m > 4 GiB` (65 537 pages) | `m` + 2 GiB + 32 MiB | the same |
| grow to `n` bytes, `n ≤` reservation − 32 MiB | in place (65 537 → 98 305 pages, no move) | commit in place |
| grow to `n` bytes beyond that | **moves**: new reservation `n` + 2 GiB + 32 MiB (98 306 pages: moved) | the same |

Wasmtime's extra 32 MiB guard *before* the base is left out (S0006 has none; explicit checks need
none); no "Limits" row changes. The 32 MiB tail is kept: the same arithmetic as Wasmtime, and the
hybrid (Q2) needs it. The 2 GiB growth reserve makes a page-by-page grower past 4 GiB move once per
2 GiB. Constants next to `MEMORY_RESERVATION`, read by i64 memories only: `RESERVATION64` = 4 GiB,
`GROWTH64` = 2 GiB, `TAIL64` = `GUARD`; the tests lower them (Test plan).

**`Memory.grow` of an i64 memory** (`instance.py:259-271` today):
1. `new = old + delta`. If it is above `max` (the declared maximum, or 2⁴⁸ − 1 pages: the byte size
   must fit the 64-bit `size_bytes` word, as in Wasmtime), return −1.
2. If `new << 16 ≤ res.size − TAIL64`: commit in place and publish the size, as today.
3. Otherwise **move**. (a) Reserve `(new << 16) + GROWTH64 + TAIL64` and commit `new << 16` in it. A
   failure returns −1, and nothing has changed. (b) Copy the old contents, **one 64 KiB page at a
   time, skipping pages that are all zero**: the new pages already read as zero. (c) Rewrite `base`
   and `size_bytes` in every bound context (`users`). (d) Free the old reservation.
4. Return `old`.

**Size check before any mapping call (a memory-safety rule).** ctypes converts an oversized int
for a `c_size_t` argument by silent truncation: `2⁶⁴ + 2 GiB` reaches C as `2 GiB` (measured
2026-11-04, CPython 3.13.17). `memory.grow` by 2⁴⁸ − 2 from 1 page asks for `2⁶⁴ − 2¹⁶ + GROWTH64 +
TAIL64` bytes. Unchecked, that becomes a 2 GiB mapping under a 2⁶⁴ − 2¹⁶ `size_bytes`, and every
bounds check passes. So `Reservation` refuses any size ≥ 2⁶³ with `OSError(ENOMEM)` before it calls
`mmap`/`VirtualAlloc` (`commit` is bounded by that size). Step 2b adds it, a test and a LESSONS
entry.

**Why the copy skips zero pages** (measured 2026-11-04 on the dev VM: 4 GiB + 64 KiB committed,
three bytes written, copied into a fresh `Reservation` with `ctypes.string_at` compares). Skip-zero
per 64 KiB page: 1.36 s, RSS +1 MiB (1 MiB chunks: 1.28 s, +3 MiB; 16 MiB: +47 MiB). One
`ctypes.memmove` of the range: 23.3 s, +4 GiB. Wasmtime copies everything: its move of a 1-page
memory grown to 65 537 pages took 24.3 s, RSS 27 MiB → 4.1 GiB. Never-written private pages read
as the shared zero page, so a move costs time linear in the size and RSS linear in the written
pages (peak: twice those). Skipping a page that was written and later zeroed is still correct.

**No stale base after a move** (checked in the code, 2026-11-04). A move happens only inside
`Memory.grow`, which runs in a libcall or in host code. Wasm code reaches both only through the
bridge, so every JIT frame is suspended at a call site. When control comes back:
* memory 0's registers are reloaded from the context after every libcall (x86-64 `call_host`,
  `backend.py:1218-1219`; aarch64 `backend.py:233`). They are also reloaded after every import
  thunk (`backend.py:1201-1202`; aarch64 `abi.py:133`), after every call through a record
  (`_call_record`, `backend.py:1106-1107`; aarch64 `backend.py:253, 257`) and in the tail switch
  (x86-64 `abi.py:124-135`; aarch64 `abi.py:107-116`);
* a direct call inside one instance does not reload (`call_direct`, `backend.py:1058-1068`). It
  needs no reload: r13/r14 always equal the current context's words, because every site that could
  change those words reloads before it returns;
* any other memory is read from its context words at each access (`_mem`, `backend.py:720-726`;
  aarch64 `backend.py:483-493`). The bulk operations hold the base in a register only inside
  their own inline loop, which calls nothing (`memory_copy`/`memory_fill`, from `backend.py:977`);
* the runtime reads `mem.res.addr` at use (`_memory_init`, `instance.py:710`; data segments,
  `:669`).

So the old reservation can be freed at the end of the grow. If an asynchronous exception
(`KeyboardInterrupt`) lands during (c), the old reservation is kept on the `Memory` instead of
being freed. That leaks it, but no context ever names unmapped memory.

**Registry.** An i64 memory is **not registered** with the guard registry. No fault point targets it
(its accesses are explicit, and bulk operations are explicit in every mode, S0006). So the handler's
condition 3 never needs it, and a move has no registry entry to replace. The loop at
`instance.py:607-608` skips i64 memories. With the hybrid (Q2) this changes, see above.

**Instantiation.** A min of 2⁴⁸ pages or more raises `MemoryError` with Wasmtime's message before
anything is reserved (Q6). A failed reservation or commit of the min raises `MemoryError("failed
to reserve N bytes")`. Wasmtime's own text names its `mmap` size, which includes its pre-guard.

**Consumers of `size_bytes` above 2³²** (checked): the word is 64-bit and holds at most
2⁶⁴ − 2¹⁶ (grow rule 1). `memory_size` shifts it in 64 bits (`backend.py:728-730`; aarch64
`:495-496`). The i32 bounds and bulk paths, `_end_check` included, never see an i64 memory
(validation types them). The i64 paths above compare in 64 bits with the carry. `_memory_init`
and the data-segment check compare Python ints.

**Host-visible consequence.** A raw address or ctypes view of an i64 memory that host code took
(`Memory.res.addr`) is invalid after any grow: the old mapping is unmapped, and using it would
crash. Wasmtime's `data_ptr` has the same rule ("valid until the next grow"). Phase 6 documents it
on the API (maintainer, 2026-11-04): host access goes through `read`/`write` methods that read the
current base at each call, as wasmtime-py's `Memory.read`/`write`; a raw pointer is documented as
valid until the next grow. No API work now. An i32 memory's address stays valid.

**Threads: the one-thread-per-store rule is enforced (maintainer, 2026-11-04).** One thread per
store (ARCHITECTURE.md). Unenforced, a host thread that grows a memory while another thread runs
that store's code gets a stale size today, and with a move a use after unmap. So the JIT store's
`_Stacks` records the owning thread when `active` goes from 0 to 1 (`instance.py`, the entry
path) and clears it when `active` returns to 0. While `active > 0`: an entry into that store's code
from another thread, and a `Memory.grow` or `Table.grow` called from another thread, raise
`RuntimeError` before anything changes, never a crash. A grow from the owning thread (from wasm or
from a host import it called) is allowed. The cost is one thread-identity compare per entry, none
per access. It covers i32 memories too (their stale size). `pyc` needs no check: its code reaches
memory through the `bytearray` object, never a raw address. Step 2b adds it with a test (a second
thread entering or growing while the first is inside a host import gets `RuntimeError`).

* **Libcall widths**: the `memory.grow` service masks its result with `M64` for an i64 memory, so a
  failure is −1 as an i64, and with `M32` for an i32 memory as today. The same applies to
  `table.grow` per table. The delta already arrives as a zero-extended 64-bit slot. Active element
  offsets are masked by the table's width (`& M32` only for i32); data offsets are not masked.
* **Context ABI**: no new word, no moved word. `size_bytes` (memories) and `size` (tables) are
  already 64-bit words, and the 2⁴⁸ − 1-page bound keeps `size_bytes ≤ 2⁶⁴ − 2¹⁶`. The amendments
  to ARTIFACT.md part 3 are text only:
  * the operand and result widths of the `memory.grow`/`table.grow`/`memory.init`/`table.init`
    libcalls for an i64 memory or table;
  * the memory row's "the base never moves" becomes: an i32 memory's base never moves (4 GiB +
    32 MiB). An i64 memory's base and `size_bytes` may both change during any host call or libcall
    (a `memory.grow` that moves it). Code keeps neither across a call: registers are reloaded
    after it, other memories are read at each access. That obligation already holds for every
    memory today.

  Part 2's `TABLES` gains the address type (`MEMORIES` already has "64-bit"). **No version bump
  (Q4, decided)**, as D37, D38 and the S0005 v128 amendment: the format and ABI v1 are not
  serialized yet, and the code obligation is unchanged. Steps 2a/2b record the amendment in
  DECISIONS.md, S0003 Changes (S0003 says "memory never moves under running code"; it still does
  not *under running code*) and S0006 Changes (its "never moves the base" is for 32-bit memories).
* **ISA, artifact flags**: baseline only (`lea`, `add`, `jc`, `cmp`, `ja`; `adds`, `subs`, `sub`,
  `cmp`, `b.cs`, `b.hi`); probably no new encoder form, and any new one is checked against
  `llvm-mc`. `BOUNDS_GUARD`/`GUARD_LOG2` keep their meaning: a guard module with an i64 memory has
  no fault points for it.

## Semantics

### Traps
An access traps with `MEMORY_OUT_OF_BOUNDS` iff `index + offset + size > size_bytes` with exact
(unbounded) arithmetic. On the JIT this means 64-bit arithmetic with the carry treated as out of
bounds; on `pyc` it means Python ints. A table access traps with `TABLE_OUT_OF_BOUNDS` iff
`index ≥ size`, and a bulk range iff its end is above the size. `call_indirect` keeps its trap
order (bounds, null, signature). A huge i64 index or offset is a trap, never an instantiation
error, a Python exception or a crash, on every target and host. On 32-bit `pyc` hosts the existing
`OverflowError` mapping covers it.

### Limits (oracle: Wasmtime 50.0.1, wasmtime-py, measured 2026-11-04 on the dev VM: 15 GiB RAM, 4 vCPU)
| case | Wasmtime 50.0.1 | JIT (both) | `pyc` |
|---|---|---|---|
| `(memory i64 0 0x1_0000_0000_0000)` | instantiates | instantiates | instantiates |
| min 2⁴⁸ pages | instantiation error "memory minimum size of 281474976710656 pages exceeds memory limits" | the same error and message (`MemoryError`, Q6) | the same |
| min 2³² pages | instantiation error "mmap failed to reserve 0x1000084000000 bytes" (min + 2 GiB growth + 2 × 32 MiB guard) | instantiation error (the 2⁴⁸ + 2 GiB + 32 MiB reservation fails on a 47-bit host) | instantiation error (`MemoryError`) |
| min 65 537 pages; min 2²⁰ pages (64 GiB) | instantiate | instantiate (address space only) | instantiates if the host can allocate it (resident); 2²⁰ pages: error on the dev VM |
| `memory.grow` from 1 page by 65 536; then by 2²⁰ | 1; then 65 537 (size 1 114 113 pages, 68 GiB, both by moving) | 1; then 65 537, both by moving | 1 (4 GiB resident); then −1 on the dev VM |
| grow by 2³², 2⁴⁸−2, 2⁴⁸, 2⁶³ | −1 each | −1 each (2⁴⁸−2: by the size check, "Runtime") | −1 each |
| `table.grow` (i64) by 10⁷ | 1 | 1 | 1 |
| `table.grow` (i64) by 10⁸; by 2⁴⁰ | **error** "out of memory (failed to allocate …)" (not −1) | −1 (D27 cap; the spec allows it) | the same |
| table64 min 2³² elements | instantiation error (out of memory) | instantiation error (D27 cap) | the same |
| an access with offset `0xFFFF_FFFF_FFFF_FFFF` | trap | trap | trap |

**Memories follow Wasmtime** (Q1 (b)). The rule is the same on every target: no cap beyond 2⁴⁸ − 1
pages, and a grow fails only when the host cannot provide the memory. The JIT matches Wasmtime's
outcomes on a 64-bit host with 47-bit user space, because it reserves the same sizes. `pyc` differs
only where its memory must be resident ("`pyc`"), so a host without the RAM fails where Wasmtime
succeeds. The spec suite does not reach either case: every grow in `memory_grow64.wast` fits in a
few pages or fails in Wasmtime too. Step 2 records the moving rule as a decision. It is not a
deviation.

**Deviation, tables only**: an i64 table keeps the D27 cap of 10 000 000 elements. A grow beyond
it is −1 where Wasmtime raises out-of-memory (`table_grow64.wast:46` is a Wasmtime xfail for that
reason). This is recorded in step 1 as D27 applied to i64 tables.
**Instantiation errors vs traps**: a min that cannot be provided is an instantiation error
(`MemoryError`, Q6), raised before the module's code runs. A failed grow is −1, never an error.

## Test plan
* **Every step**: `uv run pytest -q`; `python3 tools/check_docs.py`; `python3 tools/features.py
  --check`; the claimed runs `uv run python -m tests.spec.run --engine pyc` and `--engine
  jit-x86_64` with core numbers unchanged (plus `memory64` once claimed); `--engine wasmtime
  memory64` 8 658 pass / 2 xfail (until Q5).
* **Identity**: `uv run python tools/jit_dump.py --target {jit-x86_64,jit-aarch64}`, with
  `WABOROS_JIT_BOUNDS=guard` and with `=explicit`, before and after each step: only
  `json/memory64/` lines change or appear, and CoreMark's line is unchanged. The sha256 of `pyc`'s
  generated source for every core spec module and CoreMark is unchanged.
* **Right reason for rejections** (`tests/frontend`): every `assert_invalid` in `memory64/` raises
  `Invalid`, and every binary `assert_malformed` raises `Malformed`. **None** may raise
  `NotImplementedError`; today 286 do (Summary, Q7). Limits flags 2, 3, 6, 7 stay
  `NotImplementedError`, and ≥ 8 stays `Malformed`.
* **Spec parity**: for each JIT, `memory64` in both modes; aarch64 also under QEMU
  (`python3 tools/run_arm64.py -m tests.spec.run --engine jit-aarch64 --in-process memory64`);
  `tools/spec_diff.py pyc <jit> memory64` empty; ISA gates `tools/isa_all.py` (and
  `--target jit-aarch64`) clean.
* **Unit tests, JIT** (`tests/jit/test_memory64.py`, both backends, guard and explicit, compared
  with `pyc` and with Wasmtime):
  * every access width at index `L − size` (passes) and `L − size + 1` (traps);
  * indices `2⁶³`, `2⁶⁴ − 1` and `2⁶⁴ − size`;
  * offsets `0`, `min − size`, `min − size + 1` (the `k ≤ min` boundary), `2³¹`, `2³² + 7` and
    `2⁶⁴ − 1`, each with indices `0` and `2⁶⁴ − 1`, so that `idx + k` carries;
  * constant addresses near 2⁶⁴, the index operand as an `Imm`, a pinned `Reg`, `tos` and a `Slot`;
  * an imported i64 memory whose actual size is above its declared min;
  * `memory.fill`/`memory.copy` with `d + n` wrapping 2⁶⁴, and `memory.copy` between an i32 and an
    i64 memory;
  * multi-memory in guard mode (memory 0 i32, memory 1 i64, and the reverse): the i32 accesses are
    in the fault map, the i64 accesses are not, and both trap correctly;
  * table64: `table.get`, `set`, `fill` and `copy` and `call_indirect` at `2⁶⁴ − 1`.

  A planted `lea`-for-`add` (dropping the carry test) must fail one case.
* **Moving, JIT** (`tests/jit/test_memory64.py`, both backends, guard and explicit): marked bytes
  written before a grow are checked, with the zeros around them, after it (wasm and host), and
  compared with `pyc` and Wasmtime:
  * **mechanics, cheap** (every row, also under QEMU): `monkeypatch` sets `RESERVATION64`,
    `GROWTH64`, `TAIL64` to 1 MiB, 1 MiB, 64 KiB, so a 1-page memory moves at its 17th and 34th
    page (i32 memories keep their real reservation). Cases: a move from wasm `memory.grow` of
    memory 0 and of memory 1; a memory exported to a second instance (both contexts rewritten); a
    move in a nested entry (wasm → host → wasm grows), after which the outer frame accesses the
    memory directly and through a same-instance direct call; a host import that grows through
    `Memory`; a guard module with memory 0 i32 and memory 1 i64 (only the i32 reservation is
    registered); an injected reservation failure (−1, nothing changed); the old reservation
    unmapped afterwards;
  * **past 4 GiB, real sizes**: the "Limits" sequence (1 page; grow by 65 536, no move; by 1, a
    move to 4 GiB + 64 KiB; by 2²⁰, a move to 68 GiB of address space), then a store and load at
    `size − 8`. CI can run it because pages commit lazily and the copy skips zero pages: about 3 s
    and a few MiB of RSS on the dev VM. The test asserts RSS grew by less than 64 MiB (where
    `/proc/self/status` exists). Native rows only; under QEMU the cheap variant stands in. On
    Windows a commit is charged to the commit limit, so −1 is accepted with nothing changed;
  * **the size check**: grow by 2⁴⁸ − 2 from 1 page returns −1, and `Reservation(2⁶³)` raises
    `OSError` without mapping; a planted removal of the check must fail.
* **`pyc`**: i64 grow failure −1, also when `MemoryError` is injected; min 2⁴⁸ gives Wasmtime's
  instantiation error and allocates nothing; an injected allocation failure at instantiation gives
  `MemoryError`; a grow from 1 page by 2 pages and a check of the zeros. Nothing in pyc's tests
  allocates more than a few MiB. The 32-bit-host behaviour is covered by the existing i686/armv7
  platform cells (`memory64` in their run once `pyc` claims it).

## Plan of steps
| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | front end (`limits`, `maddr`/`taddr`, validator rules, `simd.validate` address type), `link.py` address-type check, `pyc` (typing, 4 helpers, min check), JIT `unsupported` → `"memory64"`/`"table64"`, D27 applied to i64 tables in DECISIONS.md; `pyc` claims `memory64` (no memory cap) | implementer | `--engine pyc memory64`: 8 657 pass / 0 fail / 3 xfail (`table64.wast:13`, `table_init64.wast:2457` and `:2471`) / 59 skip; rejection test (0 `NotImplementedError`); `pyc` source identity; JIT D35 lists identical (i64 modules `unsupported`) | front end ~60, `pyc` ~40, link ~8, tests ~150 |
| 2a | JIT memories: D39 amendment B (`a64` parameters), the i64 `_bounds` paths and bulk memory in both backends, the core passes `a64`, runtime (min check, grow mask; i64 reservation as an i32 one, a grow past it −1 until 2b); i64 tables still `unsupported` | jit-engineer, plan approved first | the `memory64/` files without an i64 table pass on both JITs, guard and explicit, aarch64 under QEMU; `spec_diff` empty on them; D35: only `json/memory64/` lines; ISA gates; `test_memory64.py` (bounds part) | core ~15, each backend ~70, runtime ~15, tests ~180 |
| 2b | JIT runtime: the i64 reservation rule, the move (skip-zero copy, publish, free), the `Reservation` size check, the one-thread-per-store check, no registry entry for i64 memories; the moving rule as a decision in DECISIONS.md, the ABI text amendment (ARTIFACT.md part 3) | jit-engineer, plan approved first; reviewer | "Moving, JIT" tests and the thread test on both JITs (cheap variant also under QEMU); the "Limits" memory rows equal Wasmtime's on the dev VM; D35 lists unchanged against 2a (Python only); `memory64` numbers unchanged | runtime ~60, execmem ~5, tests ~120 |
| 3 | JIT table64: `_element`, table bulk ops, `call_indirect`/`tail_indirect`, elem offsets, `table.grow` mask; both JITs claim `memory64` | jit-engineer, plan approved first | `memory64` on both JITs: 8 657 pass / 3 xfail, guard and explicit, QEMU; `spec_diff` empty; D35 as above; table tests | each backend ~40, runtime ~5, tests ~80 |
| 4 | `spectest` gains `table64` (Q5): the `table64.wast:13` entries leave all four xfail files; review of steps 2a and 3 by a `reviewer` (2b has its own) without the implementers' reasoning; docs; this spec `implemented` | reviewer, then orchestrator | 8 658 / 2 xfail (GC) on each target; core runs unchanged on all engines; findings closed | ~5 + docs |
| (5) | optional (Q2): the hybrid check, after a wasm64 CoreMark measurement on a guard row | jit-engineer | BENCHMARKS.md row; identity of i32 code; fault tests for i64 accesses | ~40 per backend |

A step that needs more than its size stops and reports. **Docs per step** (STANDARDS.md, "Which
document to update"):
* FEATURES.md, through `tools/features.py` and its notes;
* WASM-PROPOSALS.md (memory64 row → yes on all three);
* TARGETS.md (the "not done" list);
* ROADMAP.md ("Next");
* ARCHITECTURE.md (its two memory64 mentions: memory properties and the explicit-mode list; in
  2b, its JIT memory paragraph, "never moves the base", for i32 memories only);
* ARTIFACT.md, parts 2 and 3 (2a: the address type in `TABLES` and the libcall widths; 2b: the
  memory row, "the base never moves");
* S0003 Changes; S0006 Changes (i64 accesses stay explicit in guard modules; i64 reservations are
  not registered; "never moves the base" is for 32-bit memories);
* DECISIONS.md (D27 for i64 tables in step 1, D39 amendment B in 2a, the moving rule and the ABI
  text amendment in 2b);
* TESTING.md (xfail entries);
* LESSONS.md (Q7's trap, in step 1; in 2b, ctypes truncating an oversized `c_size_t`, and item 7
  refined: memory moves only inside a host call, never under running code).

## Alternatives considered
* **Address type inside the limits tuples** (`(min, max, at)` everywhere): one list fewer, but every
  consumer of `mems`, `tables` and import descriptors changes (validator, `pyc`, JIT runtime, link),
  with a risk to i32 identity. Rejected in favour of the two parallel lists, as `ttypes` already
  is.
* **A per-module attribute** (`index64: frozenset[int]`, set by the core before the first
  `func_begin`) instead of a parameter per method: fewer changed lines (no signature changes), the
  same shape as the guard mode. Rejected by the maintainer (Q3, 2026-11-04): a forgotten or stale
  attribute fails silently, compiling i32 checks for an i64 memory (out-of-bounds reads), while a
  missing required parameter fails at once.
* **Guard pages for i64 by masking the index** to the reservation: a masked out-of-bounds index
  would read inside the memory instead of trapping, which is wrong semantics (S0006 already rejected
  masking for i32).
* **A 65 536-page cap for i64 memories, never moved** (the draft's Q1 (a)). It is the smallest
  change, but it deviates from Wasmtime: a program that needs more than 4 GiB would fail on every
  target, and a later store limit would have to undo it. The code already tolerates a moving base,
  so moving costs only runtime code. Rejected by the maintainer, 2026-11-04.
* **A huge fixed reservation per i64 memory** (64 GiB to 1 TiB): growth beyond 4 GiB with no move,
  but address-space exhaustion with many live instances (PyPy frees late, LESSONS 49), `RLIMIT_AS`
  failures, and `pyc` cannot follow (RAM). Q1 (c), rejected.
* **Copy every committed byte on a move, as Wasmtime**: simplest, but it took 23.3 s and +4 GiB RSS
  for a 4 GiB memory with three bytes written ("Runtime"), against 1.4 s and +1 MiB for the
  skip-zero copy. It would make the past-4-GiB test infeasible on CI. Rejected.
* **`mremap(MREMAP_MAYMOVE)` on Linux** (moves the page tables, no copy, no read): fastest, but
  code for one OS (D47). macOS and Windows would still need the copy, and the skip-zero copy is
  already cheap enough. Not now; a row exception with a measurement could add it later.
* **Free the old reservation only when the store has no JIT entry active** (`active == 0`): that
  would turn a host thread breaking the one-thread-per-store rule into a stale read instead of a
  use after unmap. But it keeps two reservations alive during long calls (the old one with its
  written pages), and the other thread's writes are lost. Rejected (maintainer, 2026-11-04) for
  enforcing the rule at entry ("Threads").
* **Reading every bulk/table operand as 64-bit on every module** (zero-extension makes it correct):
  less code, but it changes the bytes of every i32 module (D35) for no gain. Rejected.

## Open questions
* **Q1 Limit and reservation of an i64 memory. Decided 2026-11-04: (b), "as in Wasmtime".** No
  65 536-page cap. The JIT reserves as Wasmtime and moves an i64 memory when a grow exceeds its
  reservation; `pyc` grows its `bytearray` until allocation fails. The memory rows of "Limits"
  match Wasmtime 50.0.1, and tables keep D27. (a), the cap, and (c), a huge fixed reservation, are
  in "Alternatives". Step 2b records the moving rule as a decision.
* **Q2 The hybrid check. Decided 2026-11-04: explicit checks only.** The hybrid stays optional
  step 5, taken only if a wasm64 CoreMark on a guard row shows a gain. It saves 1 instruction per
  access (`lea`), more only with large offsets.
* **Q3 How a backend learns the address width. Decided 2026-11-04: (b)**, a required keyword-only
  `a64` parameter on each method that addresses a memory or table ("Backend interface"); the
  `index64` attribute (a) is a rejected alternative.
* **Q4 Context ABI version. Decided 2026-11-04: (a), amend v1 without a bump**, as D37, D38 and
  S0005 (no artifact is serialized yet; ARTIFACT.md part 2 says no artifact is written before the
  phase-5 format spec). The amendment also covers the ARTIFACT.md sentence "the base never moves",
  which becomes i32-only (Runtime, "Context ABI"). It is recorded as a text amendment, the
  rejected alternative being (b), `ctx_abi_version` 2. The "old artifact" test that CLAUDE.md asks
  for becomes: before step 2 no i64-memory code can exist (`unsupported` refuses it, a test in step 1),
  and after it every i32 artifact is byte-identical (D35 lists), so old artifacts still run.
* **Q5 `spectest.table64`.** The reference interpreter's `spectest` exports `table64` (an i64
  table of 10 to 20 funcrefs, used by `table64.wast:13`). Wasmtime's harness lacks it, which is
  where its xfail comes from. Adding it to `tests/spec/spectest.wat` makes every engine need
  table64 to create `spectest` at all, so it can only land when all three targets have table64.
  **Decided 2026-11-04: add it in step 4**; until then the command is an xfail on every engine.
* **Q6 The exception for a min that cannot be provided. Decided 2026-11-04: `MemoryError`** with
  Wasmtime's message (for min ≥ 2⁴⁸) at instantiation, on every target; phase 6 maps it to the
  API's error, as for every other instantiation failure. A new waboros exception is rejected.
* **Q7 `NotImplementedError` counts as a passing `assert_invalid`. Decided 2026-11-04: as
  recommended.** `run.py` accepts a
  `LoadError` of kind `None` for `assert_invalid`/`assert_malformed`. In `memory64/` this hides 286
  wrong-reason passes (Summary). It matters only for a directory a target claims while part of it is
  unimplemented. Step 1's rejection test closes it for `memory64`; a LESSONS
  entry; whether the runner should count kind `None` as a failure in claimed directories is a
  separate S0001 question for the maintainer, not part of this spec.

## Changes
* 2026-11-04: created (draft).
* 2026-11-04: revised to the maintainer's answers (still draft). Q1 (b): memories follow Wasmtime,
  with no 65 536-page cap. The JIT reserves as Wasmtime (measured) and moves an i64 memory past its
  reservation, with a skip-zero copy (measured). i64 memories are not registered with the guard
  registry. `Reservation` refuses sizes ≥ 2⁶³ (ctypes truncation). The step plan splits step 2
  into 2a/2b. Q2, Q4 (including "the base never moves"), Q5 and Q6 are decided; Q3 and Q7 stay
  open.
* 2026-11-04: Q3 decided (b): a required keyword-only `a64` parameter per method instead of the
  `index64` attribute; Q7 decided as recommended.
* 2026-11-04: the one-thread-per-store rule is enforced at JIT entry and grow (step 2b); host
  pointers for phase 6; approved by the maintainer.
* 2026-11-04: step 1 implemented: `limits` flags 4 and 5, `maddr`/`taddr`, validator rules, `link.py`
  address-type check, `pyc` (`_grow64`, `_tgrow64`, `_dseg64`, `_eseg64`, and `_mem64` for the
  instantiation check), JIT `unsupported` (`memory64`/`table64`); `pyc` claims `memory64`: 8 657 pass / 0
  fail / 3 xfail / 59 skip. The tests are in `tests/test_memory64.py` (flat `tests/`, no `tests/frontend`).
* 2026-11-04: step 2a implemented: D39 amendment B in both JITs (`a64` on the eleven methods, the
  table ones asserting `not a64` until step 3; `lo` required on `load`/`store`/`simd`,
  maintainer), the i64 `_bounds` paths (`k ≤ lo` and `k < 2³¹` on x86-64, `k ≤ lo` on aarch64, else
  the carry-checked add), bulk memory with the carry, mixed copies at each side's width, the grow
  result masked by the memory's width, `MemoryError` for a min ≥ 2⁴⁸ pages. Found while testing:
  `offset + size ≥ 2⁶⁴` with a register index must be an unconditional trap (above). **Temporary
  (lifted in step 2b):** an i64 memory has the i32 reservation, so it is capped at 65 536 pages; a
  grow past it returns −1 and a larger min raises `MemoryError("failed to reserve N bytes")`.
  Results: the `memory64/` files without an i64 table pass on both JITs, guard and explicit
  (aarch64 under QEMU); `memory64-imports.wast` fails only its 30 `unsupported: table64` commands.
* 2026-11-04: step 2a review: the x86-64 `k ≤ min` path is bounded by `k < 2³¹`; at `k = 2³¹` the
  `sub rdx, k` of a memory other than memory 0 did not encode (internal `ValueError` on a valid
  module); a compile test pins it.
