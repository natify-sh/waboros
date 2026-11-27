---
title: One LIR vocabulary - the operation names of D39 as one enumerated list
kind: spec
id: S0019
status: draft
superseded_by:
related: [D17, D26, D39, D41, D53, D60, D68, S0004, S0011, S0016]
owner: maintainer
updated: 2026-11-27
---

# S0019 One LIR vocabulary: the operation names of D39 as one enumerated list

## Summary
The LIR is the D39 call interface ([DECISIONS.md](../DECISIONS.md), `jit/common/backend.py`): 54
methods. Many of them take an operation selector, such as `binop(op, ...)` or
`cmp_branch(cond, ...)`. Today that selector is a string. The string is spelled in the core (the
opcode tables of `compiler.py`, the GC lowering, `lower.py`), and each of the three backends
translates it with its own tables: `jit-x86_64`, `jit-aarch64` and `jit-sljit` (`tests/sljit/`).
Before the core is ported to C (D60, D68; ROADMAP phase 5, "Next" (6) (c)), this spec makes the
selectors **one enumerated list, defined once**. The C core then ports that list as enums, not
four naming schemes. The spec also records the review that
[SLJIT-BACKEND.md](../SLJIT-BACKEND.md) asks for: every operation's mapping onto sljit, with the
operations that need more than three sljit calls flagged, each with a proposal.

The change is a rename. No code byte changes on any target, so `tools/jit_dump.py` lists are the
acceptance (maintainer, 2026-11-25). It changes the type of an operand of D39 methods, so it is a D39
amendment: both backends in the same step, x86-64 bytes identical, a `reviewer` pass. The artifact
format, the context ABI and the ISA policy do not change, so no version changes (ARTIFACT.md).

## Goals / Non-goals
* Goals:
  * One module, `jit/common/lir.py`, defines every non-SIMD operation selector of D39 and of the
    low layer (S0011): the families, their members, their numbers, and the derived tables (`SWAP`,
    `NEGATE`, the conversion attributes).
  * Every consumer takes its names from it. No backend parses a selector string, and no
    selector passes through unchanged into an encoder's mnemonic table.
  * Code bytes identical on `jit-x86_64`, `jit-aarch64` and `jit-sljit-x86_64`, in both bounds
    modes.
  * The flagged operations below get a decision: keep, split, or move to the core.
* Non-goals:
  * No change to the D39 method set, the operand kinds, or any signature beyond the selector's type.
    Moving an operation into the core, splitting one, or adding a float register class is each its
    own later amendment if the maintainer chooses it (flags below).
  * SIMD keeps the wasm opcode as its selector (see "SIMD"). No sljit SIMD API.
  * No C header yet: the C core's spec writes it from this list.
  * No move of `tests/sljit/backend.py` onto `lower.py` (Q4).
  * `pyc`, `mpy`, HIR, the PoC and the artifact format are untouched.

## Inventory (2026-11-27, branch `gc`)

### The four naming schemes
1. **Method names** (54, the `Backend` Protocol): already one list. The low layer has 18 more
   (`ll_*`, the `LowLayer` Protocol in `lower.py`).
2. **Short selector strings** (58 names in 8 families, table below), each family passed with a
   `wide: bool`.
3. **Dotted wasm names** for `convert` (26: `"i32.trunc_sat_f64_u"`, `"f64.promote_f32"`). These
   carry their own widths, and each backend splits them back apart with `split(".")` /
   `split("_")` / `partition`.
4. **Wasm opcode integers** for `simd`: `0xFD000 + sub` (256 operators, names from
   `frontend/simd.py` `OPS`). `0x23` / `0x24` mean a v128 `global.get` / `global.set`, which are
   integers from another opcode space.

### Selector families
| family (method) | names | defined | core users | translated in |
|---|---|---|---|---|
| int condition (`cmp_set`, `cmp_branch`, `select_cmp`, `ll_jcmp`, `ll_jcmp_imm`) | `eq ne lt_s lt_u gt_s gt_u le_s le_u ge_s ge_u` (10) | `compiler.py` `_CMP`; `SWAP`, `NEGATE` in `backend.py` | `compiler.py` (pending compares, `eqz`, `ref.is_null`, `br_on_null`), `gc.py` (~20 literals), `lower.py` (5), `simd.py` (passes through) | x86-64 `CC`; aarch64 `CC`; sljit `COND`, and `_holds` parses the string again (`endswith("_s")`, `split("_")`) |
| float condition (`fcmp_set`) | `eq ne lt gt le ge` (6) | `compiler.py` `_FCMP` | `compiler.py` | an inline dict in x86-64, in aarch64; sljit `FCOND` |
| `binop` | `add sub mul and or xor shl shr_s shr_u rotl rotr` (11) | `compiler.py` `_BIN` (shared with `divrem`) | `compiler.py`, `gc.py` (~19 literals) | x86-64 `SHIFTS`, and `add sub and or xor` go straight into the encoder's `ALU` keys; aarch64 `LOGIC`, `SHIFTS`, `add sub` straight into `ADDSUB`; sljit `BIN` |
| `divrem` | `div_s div_u rem_s rem_u` (4) | in `_BIN`; routed by a name test in `compiler.py` | `compiler.py` | each backend: `op.endswith("_s")`, `op.startswith("rem")` |
| `unop` | `clz ctz popcnt extend8_s extend16_s extend32_s extend_i32_s extend_i32_u wrap` (9) | `compiler.py` `UNOPS` | `compiler.py` | string tests in each backend; `extend32_s` and `extend_i32_s` take one branch everywhere, so do `wrap` and `extend_i32_u` |
| `fbinop` | `add sub mul div min max copysign` (7) | `compiler.py` `_FBIN` | `compiler.py` | x86-64 passes it into the encoder's `SSE` keys; aarch64 into `FP2`; sljit an inline dict |
| `funop` | `abs neg ceil floor trunc nearest sqrt` (7) | `compiler.py` `_FUN` | `compiler.py` | string tests; aarch64 `rnd` dict with `get(op, op)` (`sqrt` passes into `FP1`) |
| `convert` | 26 dotted names | `compiler.py` `CONVERTS` (f-strings) | `compiler.py` | parsed in each of the three backends |
| `wide` | `add128 sub128 mul_wide_s mul_wide_u` (4) | `compiler.py` `WIDEOPS` | `compiler.py`; `WIDEOPS` keys in three `supported` sets | string tests |

Places that name LIR operations: 3 core modules plus `backend.py`, and 3 backends. The
backends' quoted tokens (x86-64 163, aarch64 121, sljit 68, counted by a regex over the names
above) are mostly **ISA mnemonics with the same spelling** (`"add"`, `"xor"`, `"sub"`), and a grep
cannot tell those from LIR names. Typed selectors make the line between the two visible. `tools/`
(`jit_dump.py`, `isa_*.py`, `artifact_dump.py`) spells none. Tests spell wasm text, not LIR names.

Other differences found:
* `here()`, `c_entry()` and `host_thunk()` return a code offset in the hand-written backends and
  a sljit mark in `jit-sljit` (its target maps marks to offsets after `finish`). The core only
  stores them and passes them back, so the contract is "an opaque position". The C port should
  say so (Q8).
* `rewind` and the far-mode retry exist for the hand-written encoders only. sljit's branches
  always reach, and its `rewind` asserts (allowed by D39: "a backend whose branches always reach
  never asks").
* `tests/sljit/backend.py` does not implement the low layer. It writes the bounds, end and
  element checks itself (`_access`, `_end_check`, `_element`) instead of going through
  `lower.py`. So the R3 rules exist twice in the repository, which departs from D53 (Q4).

## The mapping onto sljit
Counts are sljit API calls in `tests/sljit/backend.py`, operand moves and labels included. They
come from reading the code and were not measured. "custom" means `sljit_emit_op_custom` words. ✱
marks an operation above three calls or with custom words.

| operation | calls | custom | note |
|---|---|---|---|
| `new_label` `bind` `jump` `here` `poll` `trap` | 0-1 | | |
| `ret_ok` `move` `binop` `cmp_set` `cmp_branch` | 1-2 | | `binop` 1 sljit op (`MSHL`/`MASHR`/`ROTL` match wasm's masking) |
| `unop` clz ctz extend wrap | 1-2 | | sljit has `CLZ`, `CTZ`, `MOV_S8`... |
| `select_cmp` | 3-5 | | `op2u` + `sljit_emit_select`; the rest are operand moves |
| `memory_size` `ctx_load` `ctx_store` `ctx_addr` `func_type` `null_check` `landing` | 1-2 | | |
| ✱ `func_begin` / `func_end` | 8-12 / 3 per trap | aarch64 `bti jc` | the prologue of our ABI: two stack checks, the poll, R pins |
| ✱ `divrem` | 6-11 | | F1 |
| ✱ `unop` popcnt | 13 | | F2 |
| ✱ `jump_table` | about 1.3 n | | F3 |
| ✱ `fbinop` add sub mul div, `fcmp_set`, convert from int, promote/demote | 3-4 | | F4 |
| ✱ `fbinop` min max | about 20 | | F5 |
| ✱ `fbinop` copysign, `funop` abs neg | 5, 2 | | F6 |
| ✱ `funop` sqrt | 2 | 1 instruction | F7 |
| ✱ `funop` ceil floor trunc nearest | 16-22 | | F8 |
| ✱ `convert` trunc, trunc_sat | 15-25 | | F9 |
| ✱ `load` `store` | 2-3 guard, 4-7 i32, 6-9 i64 | | F10 |
| ✱ `table_get` `table_set` | 4 | | F10 |
| ✱ `memory_fill` `memory_copy` `table_fill` `table_copy` | about 25, 45, 17, 30 | | F11 |
| ✱ `simd` | sljit only for bounds and fault labels | the whole operator | F12 |
| ✱ `move_v128` | 0 | 2-6 instructions | F13 |
| ✱ `wide` | 9 (128-bit add/sub), 7 (mul_wide) | | F14 |
| `call_direct` | 2-4 | | |
| ✱ `call_record` `call_indirect` `call_ref` | 19-28 | aarch64 `stp`/`ldp` of S0 | F15 |
| `call_host` `throw_ref` | 5-6 | | |
| `tail_direct` | 2n + 1 | | argument moves |
| ✱ `tail_record` `tail_indirect` `tail_ref` | 15-22 | | F15 |
| ✱ `c_entry` `host_thunk` `c_entry_shared` | 3; 10; about 15 + helpers | stub bytes, int3 pad | F16 |
| `rewind` | | | F17 |

Low layer (S0011), for Q4: `ll_jcmp`, `ll_jcmp_imm`, `ll_add`, `ll_addr`, `ll_index32`,
`ll_index64`, `ll_index`, `ll_jump` and `ll_fault_next` are 1 call each. `ll_add_jc` and
`ll_elem_addr` are 2. `ll_three` and `ll_check_sum` are 3-4. `ll_mem`, `ll_ctx_word`,
`ll_element`, `ll_fold` and `ll_access` are 0-1 (they return an operand). So every operation of
the low layer maps onto one to four sljit calls.

### The flags and a proposal for each
"Bytes" means: would the proposal change the code of `jit-x86_64` or `jit-aarch64`?

* **F1 `divrem`: keep.** The cause is semantic: wasm traps on a zero divisor and on INT_MIN / -1,
  and no ISA does both in one instruction. sljit's `DIVMOD` also wants `SLJIT_R0`/`SLJIT_R1`. The alternative is to
  move the checks into the core with a new "trap if" method. That costs a D39 method, and x86-64
  bytes change (`test rcx, rcx` becomes a compare). Rejected.
* **F2 popcnt: keep as one operation.** The cause is the ISA level. x86-64-v1 has no `popcnt`
  (SWAR, 13 calls). aarch64 uses NEON `cnt` (4-5 instructions). x86-64-v2 would be 1 instruction
  at a declared level. A SWAR sequence in the core would freeze the slow form for every ISA.
* **F3 `jump_table`: keep.** The cause is an implementation choice. sljit has no table load that
  stays position-independent (the backend's comment), so it uses a binary search. The hand-written
  backends use a table of 32-bit offsets in the data area. The C core decides its form (Q6); the
  operation stays whole either way.
* **F4 float operations at 3-4 calls: keep.** Floats are bit patterns in integer registers (D39
  leaves a float register class open, D41), so each operation copies in and out. The single sljit
  call is there. A float register class is a non-goal here.
* **F5 min/max: keep.** The cause is semantic (NaN, -0 < +0). aarch64 does it in 1 instruction,
  SSE2 in 9, and sljit has no fmin. Lowering it in the core would cost aarch64 its single
  instruction.
* **F6 copysign, abs, neg: keep.** These are bit operations (exact NaN payloads). Moving them into
  the core as `binop` and/or is possible, but bytes change: x86-64 uses `btr`/`btc` for 64 bits,
  aarch64 the logical immediates. Rejected under this spec's acceptance. It can be revisited only
  with a measurement.
* **F7 sqrt: keep.** sljit has no square root (checked in the pinned `sljitLir.h`: no `SQRT`, no
  `POPCNT`), so the custom word is a gap in sljit, not a tie to one ISA. It is 1 instruction on
  every ISA (`sqrtsd`, `fsqrt`, `fsqrt.d`). The C core emits it per ISA.
* **F8 ceil/floor/trunc/nearest: keep as whole operations.** The cause is the ISA level (D24): no
  SSE4.1 `round*` at x86-64-v1, so 16-22 calls, while aarch64 `frint*` is 1 instruction. Only a
  whole operation lets each backend choose its form.
* **F9 trapping and saturating truncations: keep.** Semantic: range checks and traps.
* **F10 memory and table accesses: keep.** Semantic (R3). The bounds rules are `lower.py`'s on
  both hand-written backends, but `jit-sljit` re-derives them (Q4).
* **F11 bulk operators: keep.** Semantic: loops with no C library and no process address. The
  operation is the right unit for a backend that has `rep movsb` or a runtime service later.
* **F12 `simd`: keep the selector; the operation is ISA-tied on purpose.** sljit adds only the
  bounds checks, and the operator itself is our SSE2/NEON lowering as custom words (D41 native
  SSE2/NEON; D68). The pinned sljit has a partial SIMD API (`sljit_emit_simd_mov` and others).
  Using it is a later option for the C core, not part of this vocabulary.
* **F13 `move_v128`: keep.** Custom words are an implementation choice. `sljit_emit_simd_mov`
  exists and would do the 16-byte move of LESSONS 44 in 2 calls.
* **F14 `wide`: keep.** The sljit core of it is 2 calls (`ADD|SET_CARRY`, `ADDC`; `LMUL`). The
  rest is four operands and two result slots.
* **F15 calls through a record, tail calls through a record: keep.** These are ABI: D71's three
  paths and S0015's hop. aarch64's custom `stp`/`ldp` exists because a sljit frame cannot push one
  register (S0016).
* **F16 C entries: keep; they are boundary operations, outside the portable data vocabulary.**
  Their bytes are fixed by S0015 (the stub's exact form), and BTI/CET pads are ISA features sljit
  does not emit.
* **F17 `rewind`: keep as an optional protocol.** Only encoders with short branches need it.

No flag calls for a split. One naming flag does (F18 below), and two merges come out of the
inventory (F19, F20).

* **F18 `divrem` names inside `_BIN`.** The method is separate, but its names share the binop
  table and are routed by a string test. Proposal: a family of their own (`DivOp`). Bytes: none.
* **F19 duplicate `unop` names.** `extend32_s` ≡ `extend_i32_s` and `wrap` ≡ `extend_i32_u`
  (the same branch in all three backends). Proposal: one member each, `SEXT32` and `ZEXT32`
  (Q3). Bytes: none.
* **F20 `convert`'s dotted names.** Proposal: a `Conv` family whose member names keep the wasm
  spelling (`I32_TRUNC_SAT_F64_U`), plus one attribute table `CONV[c] = (dst, src, signed, sat)`
  that replaces the three parsers. Bytes: none.

## Interface (proposed)

### Where: `src/waboros/targets/jit/common/lir.py`
One module with no imports beyond `enum`, next to `backend.py`. `backend.py` imports from it, and
the `SWAP` / `NEGATE` tables move into it.

### Form: `enum.IntEnum` families, numbered from 0 in wasm opcode order
```
class Cond(IntEnum):   EQ NE LT_S LT_U GT_S GT_U LE_S LE_U GE_S GE_U        # 0..9
class FCond(IntEnum):  EQ NE LT GT LE GE                                   # 0..5
class BinOp(IntEnum):  ADD SUB MUL AND OR XOR SHL SHR_S SHR_U ROTL ROTR     # 0..10
class DivOp(IntEnum):  DIV_S DIV_U REM_S REM_U                             # 0..3
class UnOp(IntEnum):   CLZ CTZ POPCNT EXTEND8_S EXTEND16_S SEXT32 ZEXT32   # 0..6 (Q3)
class FBinOp(IntEnum): ADD SUB MUL DIV MIN MAX COPYSIGN                    # 0..6
class FUnOp(IntEnum):  ABS NEG CEIL FLOOR TRUNC NEAREST SQRT               # 0..6
class Conv(IntEnum):   I32_TRUNC_F32_S ... F64_PROMOTE_F32                 # 0..25, wasm order
class WideOp(IntEnum): ADD128 SUB128 MUL_WIDE_S MUL_WIDE_U                 # 0..3
SWAP: tuple[Cond, ...]; NEGATE: tuple[Cond, ...]       # indexed by Cond
CONV: tuple[tuple[int, int, bool, bool], ...]          # (dst type, src type, signed, sat)
V128_GLOBAL_GET, V128_GLOBAL_SET = 0x23, 0x24          # the `simd` selectors outside 0xFD000+
```
The D39 signatures change only in the selector's type: `binop(op: BinOp, ...)`,
`cmp_branch(cond: Cond, ...)`, `convert(f, op: Conv, ...)`, and so on. The same holds for
`ll_jcmp` and `ll_jcmp_imm` (`Cond`).

Why `IntEnum` (Q1):
* The value is an integer, as in the C enum it becomes. The number is the contract and the Python
  class is its spelling, so D17 holds: nothing Python-specific in what is ported.
* Each family is its own type, so pyright rejects a `Cond` passed where a `BinOp` is expected.
  Strings cannot do that, and plain integer constants cannot either.
* The names stay in reprs and asserts.
* A backend can index a tuple by the member (`CC[cond]`), the C form of a table.
* The cost: a member access (`BinOp.ADD`) is a class-attribute lookup. A micro-measurement
  (CPython 3.13.17, this host, `timeit`, not a benchmark claim) gave a dict lookup by member
  36.6 ns against 34.7 ns by string, and a tuple index by member 34.5 ns. But
  `e in (B.ADD, B.SUB)`, written inline, took 52.9 ns against 24.9 ns for strings, because the
  tuple is rebuilt on every call. Rule: a backend's sets and maps are module-level constants. The
  compile-time check in the acceptance decides.

Why 0-based per family and not one numbering across families (Q2): a backend's table is a dense
tuple per family, the C form is `static const uint8_t x86_cc[LIR_COND_COUNT]`, and the order
follows wasm's, so `Cond(op - 0x46)` stays possible. One file is still the one list.

C form (for the C core's spec, not written now): one `enum lir_<family>` per class with the same
names (`LIR_COND_LT_S`) and values, and the D39 methods as `static inline` functions taking them
(ROADMAP "Next" (6) (c)). A test that compares the header with `lir.py` belongs to that spec.

### SIMD
The selector stays the wasm opcode (`0xFD000 + sub`). It is already one enumerated list (the wasm
binary format, `frontend/simd.py` `OPS`), it is language-neutral, and it is what a C core reads
from the module. The backends key their programs by the frontend's wasm text names. That is a
lowering detail inside each backend, derived from that one list. The only change: `0x23`/`0x24`
get the names above, used by `common/simd.py` and by the backends' `emit` (Q7).

## What changes in each consumer
| file | change | lines (est.) |
|---|---|---|
| `jit/common/lir.py` (new) | the families, `SWAP`, `NEGATE`, `CONV`, the two v128 names | +110 |
| `jit/common/backend.py` | selector types in the Protocol; `SWAP`/`NEGATE` and the comment lists go (they point to `lir.py`) | ~25 |
| `jit/common/compiler.py` | `BINOPS`, `DIVOPS` (new split), `CMPS`, `UNOPS`, `F*`, `CONVERTS`, `WIDEOPS` built from the families; the `divrem` test becomes a table lookup; `"eq"`/`"ne"` literals become members | ~40 |
| `jit/common/gc.py` | about 39 literals become members (mechanical) | ~35 |
| `jit/common/lower.py`, `simd.py` | 5 conditions; the two v128 names | ~10 |
| `jit/x86_64/backend.py` | `CC`, `SHIFTS` by member; explicit `ALU_OF`, `SSE_OF` maps where the name went into the encoder; string tests become member sets; `convert` reads `CONV`; the float-condition dict | ~70 |
| `jit/aarch64/backend.py` | the same (`CC`, `LOGIC`, `SHIFTS`, `ADDSUB`/`FP2`/`FP1` maps, `rnd`, `CONV`) | ~60 |
| `tests/sljit/backend.py` | `COND`, `FCOND`, `BIN`, the `fbinop` dict, `_holds` by member, `CONV` | ~50 |
| `tests/jit/test_lir.py` (new) | each backend's tables cover their family exactly; `SWAP`/`NEGATE` are involutions with the right meaning; `CONV` agrees with the wasm names in `frontend` | +60 |

Total about 450 changed lines, mostly mechanical. The encoders (`x86_64/encoder.py`,
`aarch64/encoder.py`), the SIMD lowerings, `tools/`, `pyc` and the artifact are not touched.

## Semantics
None change. Every selector maps to the same code as its string did. The only risk is a mapping
that is not a bijection, for example a backend table that keys two members to one branch
unintentionally. The byte lists catch that on every module that uses the operation, and
`test_lir.py` catches it on the tables.

## Test plan (acceptance of the implementation step)
Before (on the base commit) and after, same host, each list diffed (expected: identical):
```
for t in jit-x86_64 jit-aarch64 jit-sljit-x86_64; do for m in explicit guard; do
  WABOROS_JIT_BOUNDS=$m uv run python tools/jit_dump.py --target $t /tmp/$t-$m.txt; done; done
uv run python tools/artifact_dump.py /tmp/art.txt          # the .wbo files, D23
```
`jit-aarch64` is cross-compiled on the x86-64 host (D35). The CI arm64 runner covers
`jit-sljit-aarch64` (its `jit_dump` list, before and after).
* `uv run python -m tests.spec.run --engine jit-x86_64` and `--engine jit-sljit-x86_64`: the same
  pass/fail/xfail counts as before (identical bytes make this a smoke check).
* `uv run pytest -q`: all pass, including `tests/jit/test_lir.py`.
* `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`: clean.
* Compile time: the standard set (CoreMark, `hash`, `mono`, `zip`), median of 10 compiles, 10
  rounds alternated with the base, `jit-x86_64` and `jit-aarch64`, both modes (BENCHMARKS.md, the
  method of "JIT low layer"). Bound: +2 %. The result is recorded in BENCHMARKS.md.
* `python3 tools/check_docs.py`, `python3 tools/features.py --check`: pass.
* A `reviewer` pass over the diff (D39).

## Plan of steps
0. **Baseline** (runner, no code): the six `jit_dump` lists, `artifact_dump`, and the compile
   time on the base commit.
1. **The vocabulary and every consumer, one commit** (implementer; about 450 lines). D39 requires
   both backends in the same step, and the Protocol's types must check under pyright at once, so
   the change cannot be split by backend. Inside the step the order is: `lir.py` and `test_lir.py`,
   then the core, then the three backends, diffing the lists after each backend.
2. **Docs** (part of step 1's commit): D39's amendment list in DECISIONS.md (the selector types,
   this spec); ARCHITECTURE.md "The IR" (one sentence: the vocabulary is `lir.py`); S0011's "Conditions are D39's
   names" (now `lir.Cond`); SLJIT-BACKEND.md's proposal paragraph (done: this spec); the ROADMAP
   item; the spec index.
3. **Review** (`reviewer`): read the diff against this spec, check each table's bijection, check
   the lists.
Not in this plan: Q4 (`jit-sljit` onto `lower.py`), which is its own step if the maintainer wants
it before the C core.

## Alternatives considered
* **`typing.Literal` strings plus one tuple per family.** Pyright checks each `Literal`, and the
  values stay strings, so the diff is smaller (about 200 lines). The cost: the C port still
  needs a name-to-number table, the three `convert` parsers stay unless `CONV` is added anyway,
  and an encoder's keys still look like LIR names. It is the fallback if the compile time of
  `IntEnum` misses the bound.
* **Plain integer constants** (`ADD = 0`). Fastest and closest to C, and the only form
  MicroPython could run, which the JIT core does not need (no ctypes there). The cost: no type
  per family and no names in reprs, so a `Cond` passed as a `BinOp` goes unnoticed.
* **One numbering across all families** (disjoint ranges). One C enum, but sparse tables (Q2).
* **Materialising the LIR as a list** (D26 rejected it): not needed to fix the names.

## Open questions
* **Q1.** `IntEnum` (proposed), `Literal` strings, or plain integers?
* **Q2.** Per-family numbering from 0 (proposed), or one numbering across families?
* **Q3.** Merge `extend32_s`/`extend_i32_s` into `SEXT32` and `wrap`/`extend_i32_u` into `ZEXT32`
  (proposed; no byte changes)? Or keep the wasm-derived names for traceability?
* **Q4.** `jit-sljit` re-derives the R3 checks instead of implementing the 18 `ll_*` operations,
  each of which maps onto 1-4 sljit calls. Should it move onto `lower.py` before the C core (a
  separate step, about −120/+80 lines in `tests/sljit/backend.py`, `jit-sljit` bytes change,
  acceptance by behaviour: the spec suite and `spec_diff`), so that the C core ports one bounds
  implementation? Or should the C core do that when it is written? Is the low layer part of the
  vocabulary the C core ports? (Proposed: yes, as its second tier, by method name.)
* **Q5.** Of F1-F17, does the maintainer want any moved into the core now? Proposed: none. F6
  (copysign/abs/neg as `binop`) is the only one that is semantically possible, and it changes
  bytes.
* **Q6.** `jump_table` in the C core: keep the binary search, or a position-independent table
  (label differences in the data area) where sljit allows it? To decide in the C core's spec.
* **Q7.** The v128 globals: named constants under `simd` (proposed), or two methods of their own
  (a D39 amendment, both backends, bytes unchanged)?
* **Q8.** Write D39's `here()` / `c_entry()` / `host_thunk()` result as "an opaque position the
  backend maps to an offset in `finish`" (what `jit-sljit` already does)? It is a text change,
  for the C core.

## Changes
* 2026-11-27: created (draft): inventory, sljit mapping, flags, proposal.
