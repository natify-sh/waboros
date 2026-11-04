---
title: memory64 and table64 (Wasm 3.0) on pyc, jit-x86_64 and jit-aarch64
kind: spec
id: S0009
status: draft
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

Main choices (each one is an open question below; the recommendation is in **bold**):
* The front end records the address type in two new lists. Instruction tuples and limits tuples
  stay as they are, so i32 code on every target is unchanged **by construction**.
* `pyc` needs only typing and four small helpers. Its addresses are already Python ints, and the
  errors an out-of-range address raises are already mapped to traps on 64- and 32-bit hosts.
* The JIT uses **explicit checks only** for i64 memories, in both bounds modes, written the way
  Wasmtime writes them: 3 instructions per access in the common case. The guard-page trick cannot
  cover a 64-bit index (see "Why guard pages cannot cover an i64 index"). The Wasmtime-style hybrid
  (guard region plus one compare) is described as an option, **deferred until a measurement**.
* An i64 memory gets **the same reservation as an i32 memory** (4 GiB + 32 MiB, never moved) and the
  same 65 536-page limit on every target. This **deviates from Wasmtime**, which grows such a memory
  beyond 4 GiB by moving it (measured below).
* **Context ABI v1 is amended, not bumped**: no new word. The libcalls `memory.grow` and `table.grow`
  return −1 at the address width.

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
* Non-goals: guard pages for i64 memories (the hybrid is optional, Q2); memories above 65 536 pages
  or moving memories (Q1); shared memories and limits flags 2, 3, 6 and 7 (threads; they stay
  `NotImplementedError`); custom page sizes; `memory.discard`; `mpy`; the public API (`Config`,
  store limits: phase 6); a speed claim (none is made).

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
  reports `"memory64"`, one with an i64 table reports `"table64"`. Steps 2 and 3 remove these.

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
* `memory.grow` on an i64 memory: `_grow64(M, maxpages, n)` returns `0xFFFFFFFFFFFFFFFF` on failure,
  and also when `bytes(n << 16)`/`extend` raises `MemoryError` or `OverflowError`. `maxpages` is
  `min(max, 65536)` ("Limits"). `table.grow`: `_tgrow64`, the same cap of 10 000 000 elements as
  D27, −1 as `M64`.
* Active segments: `_dseg64`/`_eseg64` take the 64-bit offset without the `& 0xFFFFFFFF` of
  `_dseg`/`_eseg`. `memory.copy`, `memory.fill`, `memory.init` and the table helpers compare with
  Python ints and need no change.
* Memory creation: an i64 memory whose min is above 65 536 pages fails **at instantiation** with
  the "Limits" error. The generated line for an i64 memory checks before it allocates; module
  definitions do not allocate, so `memory64.wast:8` (a definition with min 2⁴⁸) passes.
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
  1). If the declared maximum makes the access impossible (`k > max·65536`), the backend emits
  `jmp oob`. That decision uses **only the module's declared limits, never the runtime's cap**, so
  an artifact stays correct for a host with a larger limit (D36).
* **Constant address** (`Imm`): `ea = value + offset` without the `& M32` of the i32 path (that mask
  would be a silent wrong-address bug). `ea + size > 2⁶⁴` gives `jmp oob`; otherwise it compares with
  `L` as the i32 path does (`cmp L, imm` when below 2³¹, else through `rdx`/`x10`).
* The access: `[r13 + idx + offset]` (x86-64; `offset` in the displacement when it is below 2³¹,
  otherwise added through `rdx`), `[x26, x9]` after adding `offset` (aarch64).
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
fault map lists i32 accesses only. Every memory keeps its reservation, which a guard module already
registers (`guard.add_reservation`), so nothing changes in the handler or its rows (D47).

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
  obligation. **No reservation cost**: the i64 memory has the same 4 GiB + GUARD reservation as an
  i32 one (Q1 (a)). With a moving base (Q1 (b)) the registry entry would be replaced at each move
  (`add_reservation` and `_forget` exist). The code itself is unaffected, because it rereads the
  base after every call (below).
* **Recommendation (Q2)**: explicit only in this spec. The hybrid is an optional later step, taken
  only if a wasm64 CoreMark run on a guard row shows a gain worth the second path.

### Backend interface: D39 amendment B (both backends, one step, reviewer)
```python
class Backend(Protocol[L, F]):
    index64: frozenset[int]  # context words (memory `base`, table `base`) whose address type is i64
```
The core sets `index64` once per module, before the first `func_begin`, from `maddr`/`taddr` and the
`Layout`. The default `frozenset()` means i32 everywhere, so an i32-only module goes down exactly
today's paths. **Contract**: for a word in `index64`, the address or index operands of the methods
that name that word are full 64-bit values. Those methods are `load`, `store`, the SIMD memory
operators (the memory word in `imm`), `memory_fill`, `memory_copy` (each side by its own word, `n`
64-bit only when both are), `table_get`, `table_set`, `table_fill`, `table_copy`, `call_indirect`
and `tail_indirect`. For any other word, operands stay i32 as today. The core never passes an
`AddK` for an `index64` word. `memory_size` and the table-size `ctx_load` are unchanged (both words
are already 64-bit). No method or signature changes. Under D39 this is still an amendment, because
the meaning of operands changes. So it needs maintainer approval, both backends in the same step,
x86-64 byte identity for every module without an i64 memory or table, and a `reviewer` pass.

**Table64 on the JIT**: `_element` loads the index with a 64-bit move and compares it with the
64-bit `size` word (`cmp; jae`, no carry possible). `table_fill`/`table_copy` use the carry-checked
end check. `call_indirect` and `tail_indirect` take the table record through `_element`. The
`table.grow` and `table.init` libcalls receive 64-bit slots already.

### Runtime (`jit/common/instance.py`) and the context ABI
* **Reservation (Q1 (a))**: an i64 memory is a `Memory` like any other: 4 GiB + GUARD reserved,
  committed on grow, **never moved**, `max = min(declared, 65 536)` (`MAX_PAGES`). If its min is
  above 65 536 pages, instantiation fails with the "Limits" error before anything is reserved.
* **Moving bases**: the code already tolerates them. Memory 0's registers (r13/r14, x26/x25) are
  reloaded after every call and every `memory.grow`, and any other memory is reread from its words
  at each access (ARTIFACT.md part 3). What a move would change is runtime and contract: copying the
  committed bytes, rewriting `base` in every bound context (`users`), re-registering for the
  handler, and the ARTIFACT.md sentence "the base never moves". That is Q1 (b), not this spec.
* **Libcall widths**: the `memory.grow` service masks its result with `M64` for an i64 memory, so a
  failure is −1 as an i64, and with `M32` for an i32 memory as today. The same applies to
  `table.grow` per table. The delta already arrives as a zero-extended 64-bit slot. Active element
  offsets are masked by the table's width (`& M32` only for i32); data offsets are not masked.
* **Context ABI**: no new word, no moved word. `size_bytes` (memories) and `size` (tables) are
  already 64-bit words. With the 65 536-page cap, `size_bytes ≤ 2³²`, so a 2⁶⁴-byte memory (which
  would not fit the word) cannot occur. The amendments to ARTIFACT.md part 3 are text only: the
  operand and result width of the `memory.grow`/`table.grow`/`memory.init`/`table.init` libcalls
  for an i64 memory or table, and "i64 memories follow the same reservation rule". Part 2's
  `TABLES` gains the address type (`MEMORIES` already has "64-bit"). **No version bump (Q4)**, as
  D37, D38 and the S0005 v128 amendment: the format and ABI v1 are not serialized yet. The step
  records the amendment in DECISIONS.md and S0003 Changes.
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
| case | Wasmtime 50.0.1 | this spec, all three targets |
|---|---|---|
| `(memory i64 0 0x1_0000_0000_0000)` | instantiates | instantiates (max capped at 65 536 pages internally) |
| min 2⁴⁸ pages | instantiation error "memory minimum size of 281474976710656 pages exceeds memory limits" | instantiation error (same message) |
| min 2³² pages | instantiation error "mmap failed to reserve 0x1000084000000 bytes" (min + 2 GiB growth + 2 × 32 MiB guard) | instantiation error |
| min 65 537 pages; min 2²⁰ pages (64 GiB) | instantiate | **instantiation error (deviation)** |
| `memory.grow` from 1 page by 65 536; then by 2²⁰ | 1; then 65 537 (size 1 114 113 pages, 68 GiB, by moving) | 1; then **−1 (deviation)** |
| grow by 2³², 2⁴⁸−2, 2⁴⁸, 2⁶³ | −1 each | −1 each |
| `table.grow` (i64) by 10⁷ | 1 | 1 |
| `table.grow` (i64) by 10⁸; by 2⁴⁰ | **error** "out of memory (failed to allocate …)" (not −1) | −1 (D27 cap; the spec allows it) |
| table64 min 2³² elements | instantiation error (out of memory) | instantiation error (D27 cap) |
| an access with offset `0xFFFF_FFFF_FFFF_FFFF` | trap | trap |

**Deviation, recorded as a new decision with this spec**: i64 memories are limited to 65 536 pages
(4 GiB), like i32 memories, on every target (D25). The reasons: `pyc` allocates a `bytearray` (a
4 GiB grow already touches about 8 GiB), and the JIT's memory never moves (LESSONS 7). Wasmtime grows
past 4 GiB by moving the memory (`memory_may_move`, `memory_reservation_for_growth`). The spec
suite does not see the difference: every grow in `memory_grow64.wast` either fits in a few pages or
fails in Wasmtime too. The limit becomes a store limit in phase 6, as D27 does for tables.
**Instantiation errors vs traps**: a min above the cap is an instantiation error, raised before
anything is allocated, with Wasmtime's message; its exception type is Q6. A failed grow is −1,
never an error.

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
* **`pyc`**: i64 grow failure −1, also when `MemoryError` is injected; min above the cap gives the
  instantiation error and allocates nothing; the 32-bit-host behaviour is covered by the existing
  i686/armv7 platform cells (`memory64` in their run once `pyc` claims it).

## Plan of steps
| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | front end (`limits`, `maddr`/`taddr`, validator rules, `simd.validate` address type), `link.py` address-type check, `pyc` (typing, 4 helpers, min check), JIT `unsupported` → `"memory64"`/`"table64"`, the limits decision in DECISIONS.md; `pyc` claims `memory64` | implementer | `--engine pyc memory64`: 8 657 pass / 0 fail / 3 xfail (`table64.wast:13`, `table_init64.wast:2457` and `:2471`) / 59 skip; rejection test (0 `NotImplementedError`); `pyc` source identity; JIT D35 lists identical (i64 modules `unsupported`) | front end ~60, `pyc` ~40, link ~8, tests ~150 |
| 2 | JIT memories: D39 amendment B (`index64`), the i64 `_bounds` paths and bulk memory in both backends, the core sets `index64`, runtime (min check, grow mask); i64 tables still `unsupported` | jit-engineer, plan approved first | the `memory64/` files without an i64 table pass on both JITs, guard and explicit, aarch64 under QEMU; `spec_diff` empty on them; D35: only `json/memory64/` lines; ISA gates; `test_memory64.py` (memory part) | core ~15, each backend ~70, runtime ~15, tests ~180 |
| 3 | JIT table64: `_element`, table bulk ops, `call_indirect`/`tail_indirect`, elem offsets, `table.grow` mask; both JITs claim `memory64` | jit-engineer, plan approved first | `memory64` on both JITs: 8 657 pass / 3 xfail, guard and explicit, QEMU; `spec_diff` empty; D35 as above; table tests | each backend ~40, runtime ~5, tests ~80 |
| 4 | `spectest` gains `table64` (Q5): the `table64.wast:13` entries leave all four xfail files; review of steps 2–3 by a `reviewer` without the implementers' reasoning; docs; this spec `implemented` | reviewer, then orchestrator | 8 658 / 2 xfail (GC) on each target; core runs unchanged on all engines; findings closed | ~5 + docs |
| (5) | optional, only if Q2 is approved: the hybrid check, after a wasm64 CoreMark measurement on a guard row | jit-engineer | BENCHMARKS.md row; identity of i32 code; fault tests for i64 accesses | ~40 per backend |

A step that needs more than its size stops and reports. **Docs per step** (STANDARDS.md, "Which
document to update"):
* FEATURES.md, through `tools/features.py` and its notes;
* WASM-PROPOSALS.md (memory64 row → yes on all three);
* TARGETS.md (the "not done" list);
* ROADMAP.md ("Next");
* ARCHITECTURE.md (its two memory64 mentions: memory properties and the explicit-mode list);
* ARTIFACT.md, parts 2 and 3 (step 2: the address type in `TABLES` and the libcall widths);
* S0003 Changes; S0006 Changes (i64 accesses stay explicit in guard modules);
* DECISIONS.md (the limits deviation in step 1, D39 amendment B in step 2, the ABI text amendment);
* TESTING.md (xfail entries);
* LESSONS.md (Q7's trap, in step 1).

## Alternatives considered
* **Address type inside the limits tuples** (`(min, max, at)` everywhere): one list fewer, but every
  consumer of `mems`, `tables` and import descriptors changes (validator, `pyc`, JIT runtime, link),
  with a risk to i32 identity. Rejected in favour of the two parallel lists, as `ttypes` already
  is.
* **One parameter per backend method** (`a64: bool` on ~11 methods) instead of `index64`: explicit
  at every call, and the core decides. It changes 11 signatures in two backends and in every
  future backend (riscv64, sljit); the per-module set is one attribute, like the guard mode, which
  is constructor state. Q3.
* **Guard pages for i64 by masking the index** to the reservation: a masked out-of-bounds index
  would read inside the memory instead of trapping, which is wrong semantics (S0006 already rejected
  masking for i32).
* **A huge fixed reservation per i64 memory** (64 GiB to 1 TiB): growth beyond 4 GiB with no move,
  but address-space exhaustion with many live instances (PyPy frees late, LESSONS 49), `RLIMIT_AS`
  failures, and `pyc` cannot follow (RAM). Q1 (c).
* **Moving on grow, as Wasmtime**: the code tolerates it already, but it is a runtime and contract
  change, with copying and re-registration. It is not needed by the spec suite. Q1 (b), later.
* **Reading every bulk/table operand as 64-bit on every module** (zero-extension makes it correct):
  less code, but it changes the bytes of every i32 module (D35) for no gain. Rejected.

## Open questions
* **Q1 Limit and reservation of an i64 memory.** (a) The i32 reservation and a cap of 65 536 pages
  on all targets; never moved; a new decision records the deviation from Wasmtime. (b) Move the
  memory when a grow exceeds its reservation, as Wasmtime (`memory_may_move`): runtime only (copy,
  rewrite `base` in every bound context, re-register), plus the ARTIFACT.md text "the base never
  moves" changed for i64 memories (a contract change: Q4 applies again); `pyc` would still need its
  own cap. (c) Reserve min(declared max, 64 GiB or more). **Recommendation: (a) now; (b) when a
  real program needs more than 4 GiB, with its own measurement.**
* **Q2 The hybrid check.** **Recommendation: explicit only now** (3 instructions per access in the
  common case, the same count as i32 explicit). The hybrid becomes optional step 5, taken only if a
  wasm64 CoreMark on a guard row shows a gain. It saves 1 instruction per access (`lea`), more only
  with large offsets.
* **Q3 How a backend learns the address width.** (a) The `index64` attribute (D39 amendment B, no
  signature change). (b) A parameter per method. **Recommendation: (a).**
* **Q4 Context ABI version.** (a) An amendment of v1 without a bump, as D37, D38 and S0005 (no
  artifact is serialized yet; ARTIFACT.md part 2 says no artifact is written before the phase-5
  format spec). (b) Bump `ctx_abi_version` to 2. **Recommendation: (a).** The "old artifact" test that CLAUDE.md asks
  for becomes: before step 2 no i64-memory code can exist (`unsupported` refuses it, a test in step 1),
  and after it every i32 artifact is byte-identical (D35 lists), so old artifacts still run.
* **Q5 `spectest.table64`.** The reference interpreter's `spectest` exports `table64` (an i64
  table of 10 to 20 funcrefs, used by `table64.wast:13`). Wasmtime's harness lacks it, which is
  where its xfail comes from. Adding it to `tests/spec/spectest.wat` makes every engine need
  table64 to create `spectest` at all, so it can only land when all three targets have table64.
  **Recommendation: add it in step 4**; until then the command is an xfail on every engine.
* **Q6 The exception for "min above the cap".** (a) `MemoryError` with Wasmtime's message, raised by
  both targets at instantiation. (b) A new waboros exception before the phase-6 API. **Recommendation:
  (a)**; phase 6 maps it to the API's error, as for every other instantiation failure.
* **Q7 `NotImplementedError` counts as a passing `assert_invalid`.** `run.py` accepts a
  `LoadError` of kind `None` for `assert_invalid`/`assert_malformed`. In `memory64/` this hides 286
  wrong-reason passes (Summary). It matters only for a directory a target claims while part of it is
  unimplemented. **Recommendation:** step 1's rejection test closes it for `memory64`; a LESSONS
  entry; whether the runner should count kind `None` as a failure in claimed directories is a
  separate S0001 question for the maintainer, not part of this spec.

## Changes
* 2026-11-04: created (draft).
