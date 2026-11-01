---
title: Fixed-width SIMD (v128) on pyc, jit-x86_64 and jit-aarch64
kind: spec
id: S0005
status: draft
superseded_by:
related: [D22, D23, D24, D25, D26, D28, D33, D35, D36, D37, D39, R2, R3, S0001, S0002, S0003, S0004]
owner: maintainer
updated: 2026-11-01
---

# S0005 Fixed-width SIMD (v128) on pyc, jit-x86_64 and jit-aarch64

## Summary
Wasm SIMD (the `simd` feature, part of Wasm 2.0: the `v128` type and 236 operators under the `0xFD`
prefix) on the three targets, in the maintainer's order: `pyc` first (correct before fast), then the
JITs. This spec fixes the v128 value model on each target, how v128 crosses the entry, host and
global boundaries, the amendment of the frozen backend interface (D39) it needs, the SSE2 and NEON
lowering at the ISA baselines (D24), NaN rules, how a target claims the `simd/` spec directory, and
the steps. Requirements set by the maintainer (binding):

1. **SIMD lives in its own modules**: `src/w2py/targets/pyc/simd.py`,
   `src/w2py/targets/jit/x86_64/simd.py`, `src/w2py/targets/jit/aarch64/simd.py`. Existing files only
   dispatch to them; every inline hook is listed in "Hooks" below, nothing else changes.
2. **`simd/` stays unclaimed by a target until it nearly passes** (threshold in "Claiming `simd`");
   no large expected-failure lists.
3. **Order**: `pyc` first, then the JITs.

Ideas read (not copied): Cranelift x64 lowering (`cranelift/codegen/src/isa/x64/lower.isle`, main,
read 2026-11-01) and aarch64 lowering; V8's x64 assembler (`CpuFeatures::SupportsSimd128`); pywasm
(v128 as a 16-byte `bytearray`, lanes by `struct`). wasm3 has no SIMD.

## Goals / Non-goals
* Goals: the `simd/` directory (25 481 runnable commands; 509 text-format `assert_malformed` are
  skipped for every engine, S0001) passes on `pyc`, `jit-x86_64` and `jit-aarch64` and is claimed
  by each; JIT code for SIMD uses only x86-64-v1 (SSE2) and ARMv8.0-A NEON; code and generated
  source of every module without v128 stay **byte-identical**; CoreMark does not move.
* Non-goals: relaxed SIMD (`relaxed-simd/`, 79 commands: a follow-up spec), x86-64-v2/v3 variants
  (D33 opt-in, after a measurement, Q4), a float or vector register class (Q3), `mpy` (tier 3,
  D22), NaN canonicalization as an option (Q5), the public API.

## Interface

### Front end (shared by every target)
`frontend/binary.py` decodes `0xFD` + LEB128 sub-opcode into the instruction tuple
**`(0xFD000 + sub, immediates...)`** (Q6: `0xFD00 + sub` would collide with a later `0xFE` prefix,
sub-opcodes reach `0x113` with relaxed SIMD). Immediates: memory ops `(offset, align, mem)` as
scalar loads (D37: memory index last); lane ops `(lane,)`; lane memory ops
`(offset, align, mem, lane)`; `v128.const` `(value,)` as an int in `[0, 2^128)`, little-endian (lane 0 in
the low bits); `i8x16.shuffle` `(lanes,)` as a 16-byte `bytes`. `v128.const` is also a constant
expression `("v128", value)`. The validator types every operator from one table in the new
`frontend/simd.py` (sub-opcode → name, immediate kind, operand types, result type, lane count for
the `lane < n` check) and **annotates the two polymorphic operators** when their operand is v128:
`drop` becomes `(0x1A, 0x7B)` and untyped `select` becomes the typed form `(0x1B, [0x7B])`. Tuples of
modules without v128 do not change.

### v128 per target
| | `pyc` | JIT (both architectures) |
|---|---|---|
| value | Python `int` in `[0, 2^128)`, the 16 bytes little-endian (the runner's and the Wasmtime adapter's form, S0001) | 16 bytes in **two consecutive slots** k, k+1: bytes 0–7 in k, 8–15 in k+1 (ARTIFACT.md already says "v128 takes two slots") |
| locals, operand stack | an `int` like any other; locals start at `0` | a v128 local or stack entry is **two 64-bit halves** for every generic path of the core (flush, branch moves, calls, block params); only SIMD operations see a pair |
| globals | an `int` | 2 context words (ARTIFACT.md: "1 word (2 for v128)"); an imported one: a pointer to the owner's 2 words |
| host functions, entry | `int` in and out, no conversion (`_from_raw`/`_to_raw` pass it through) | `JitInstance.call`, the host callback and global init/get split a v128 into `(lo, hi)` slots and join them back |
| lane access | `int.to_bytes(16, "little")` + a `struct.Struct` per shape, back with `int.from_bytes`; bitwise ops directly on the `int` | in the backend's `simd` method, vector registers as scratch (below) |

Measured (Xeon 2.1 GHz, 4 cores, CPython 3.13.17, one session, `timeit` min of 5 × 200 000):
`i32x4.add` on an `int` through `struct` 1 061 ns, on `bytes` through `struct` 798 ns, on an `int`
by SWAR (`((a & L) + (b & L)) ^ ((a ^ b) & H)`) 159 ns, on a tuple of lanes 224 ns; `v128.xor` on an
`int` 66 ns, on `bytes` 258 ns. The `int` wins on bitwise ops, constants, `select`, globals and the
boundary (no conversion), and leaves SWAR as a measured optimisation for later; `struct` first.

### Context ABI and artifact: no version bump
v128 at the boundary is what ARTIFACT.md part 3 (version 1) already states: two slots for a
parameter or result, two words for a global. The spec adds one clarifying sentence to ARTIFACT.md
part 3: "the low 64 bits (bytes 0–7 of the v128) in the first slot or word". `Layout` takes the
global widths (default 1 each; a defined v128 global 2, an imported one 1: the pointer), so only
modules with a v128 global move later words — modules refused (`unsupported: simd`) until now.
Frames, records, libcalls, trap codes, trampolines and the header are unchanged; the ISA level stays the baseline (`isa_level` 1, no feature bits). As
with D37 and D38 this is a v1 amendment without a bump (no artifact is saved before phase 5, D36);
after phase 5 the same change would bump `ctx_abi_version`.

### D39 amendment: the backend interface
**No register class.** v128 values live in slots between backend calls; a backend loads them into
vector registers inside one call and stores the result. Changes to the frozen interface (they need
the maintainer's approval, both backends in one step, x86-64 code byte-identical for modules without
v128, a `reviewer` pass):

1. **Operand meaning for v128.** In `simd`, a v128 operand is `Slot(k)` (slots k, k+1, 8-byte
   aligned, so unaligned 16-byte accesses: `movdqu`, `ldr q`/`ldur q` or an `add` first) or `Imm(v)`
   with the 128-bit value; a v128 result is `Slot(k)` (never `tos`). Scalar operands and results
   (lane values, shift counts, addresses, `extract_lane`, `bitmask`, `any_true`, `all_true`) are as
   today: `tos`, a pool register, a slot or an `Imm`, zero-extended.
2. **One new method** (the only one; a method only new opcodes reach would not by itself be an
   interface change under D39, the operand meaning above is):
   ```python
   def simd(self, f: F, op: int, dst: Dst, args: tuple[Operand, ...], imm: tuple[int, ...]) -> None
   ```
   `op` = `0xFD000 + sub` (or `0x23`/`0x24` for a v128 global, `imm = (word, indirect)`); `args` in wasm
   operand order; `imm`: lane, the shuffle lanes as one 128-bit int, `(offset, memory word)` for memory
   forms. Contract: reads every operand before writing `dst` (`dst` may be the first operand's
   slots); memory forms check bounds as `load`/`store` do (`MEMORY_OUT_OF_BOUNDS`); preserves pool,
   frame, context, memory-0 registers and `tos` unless `dst` is `tos`; leaves no flags and no vector
   register live; may use the **scratch slots** `base + 6 … base + 9` (32 bytes; `base` = `L` + the
   stack height after the operands are popped, so up to three v128 operands fit below), which the
   core counts in `frame_slots`.
3. **`supported`** gains the `0xFD000 + sub` codes a backend compiles; `unsupported()` returns
   `"simd"` for a v128 type only when the backend's set has no SIMD opcode, else the first missing
   opcode, as today.
4. **The core's frame layout**: slot of local j = sum of the widths of locals before j (v128 = 2,
   else 1), arities of blocks, calls and returns in slots. Identical numbers when no v128 exists.

**Vector registers** (scratch inside one `simd` call; none is live across a call or between calls):

| | scratch | never used | why |
|---|---|---|---|
| x86-64 | `xmm0`–`xmm5` (floats keep `xmm0`–`xmm2`, also scratch) | `xmm6`–`xmm15` | Win64: xmm6–15 are callee-saved; using them would make the `win64` entry save 160 bytes; SysV: all caller-saved. One artifact for both (D23) |
| aarch64 | `v0`–`v7`, `v16`–`v31` | `v8`–`v15` | AAPCS64: the low halves `d8`–`d15` are callee-saved (Linux, Apple, Windows) |

So no trampoline changes and no save around host calls (the bridge saves GPRs only, as today):
nothing vector-sized is live when the bridge runs. Spills do not exist: the slot is the home.

**Byte-identical**: the core reaches `simd`, the slot map and the annotations only through v128
types or `0xFD` opcodes; the D35 lists (`tools/jit_dump.py`) of every spec module without v128 and
CoreMark are identical before and after for both targets (step acceptance).

### Hooks (the complete list of inline changes outside the new modules)
| file | change |
|---|---|
| `frontend/binary.py` | `0x7B` out of `UNSUPPORTED_TYPES`, `0xFD` out of `UNSUPPORTED_OPS`; `decode`: `0xFD` → `simd.decode(r)`; constant expression `v128.const` |
| `frontend/validate.py` | value type `0x7B`; `op >= 0xFD000` → `simd.validate(...)`; the `drop`/`select` annotation; `v128.const` as a constant |
| `targets/pyc/codegen.py` | `simple()`: `op >= 0xFD000` → `simd.gen(s, ins)`; `drop`/`select` need nothing (an `int`); `val()` for `("v128", v)` |
| `targets/pyc/__init__.py` | none (v128 passes as an `int`); the generated module imports `simd`'s helpers like the runtime's |
| `jit/common/compiler.py` | `type_ok`/`unsupported` (item 3); slot map and arities in slots (item 4: `loc`, `run`'s local init, `enter`, `call*`, `tail_call`, the function's `_Ctl`); `drop`/`select` of v128; `global.get/set` of v128; `op >= 0xFD000` → `jit/common/simd.py` |
| `jit/common/context.py` | `Layout` global widths |
| `jit/common/instance.py` | split/join of v128 in `call`, `_host`, global init and `global_get` (helpers in `jit/common/simd.py`) |
| `jit/common/backend.py` | the `simd` method in the protocol |
| `jit/x86_64/backend.py`, `jit/aarch64/backend.py` | `simd` delegates to the module; `supported` adds the module's set |
| `tools/isa_check.py` | the SSE2 packed mnemonics join the x86-64-v1 allowlist (`pshufb`, `roundps`, `pminsd`, `pmulld`, … stay refused) |
| `tests/spec/claims.py` | `simd` per target when claimed |

The new shared modules — `frontend/simd.py` (the one operator table) and `jit/common/simd.py` (stack
effects, operand pairs, select, globals, the boundary split) — are beyond the maintainer's list of
three (Q8). Instruction encodings for SIMD live in the architecture's `simd.py` on top of the
encoder's raw emit; `encoder.py` files do not change.

## Semantics

### pyc
Bitwise ops (`not and andnot or xor bitselect any_true`) on the `int`; every lane op through
`struct` shapes `<16b <16B <8h <8H <4i <4I <2q <2Q`. Float lanes: data movement and sign ops
(`splat`, lanes, `abs`, `neg`, loads, stores, shuffles) work on the **bits**, never through a Python
float (CPython's float↔`f` conversion may quiet a signalling NaN). Arithmetic reuses the scalar
`pyc` helpers lane by lane (D28: f32 lanes in doubles, exact for `+ - * / sqrt`; `min`/`max`,
rounding and `trunc_sat` with the scalar rules), and an f32 lane enters or leaves as a scalar
exactly as `pyc`'s `f32` values do (D28 payload rule).

### JIT, x86-64-v1 (SSE2 only)
**What others do without SSE4.1** (checked 2026-11-01): Wasmtime's `Config::wasm_simd` says "the
base CPU feature requirement for SIMD is SSE2 for the Cranelift compiler and AVX for Winch"; on an
SSE2-only CPU Cranelift lowers `pshufb` (general shuffle, swizzle) and vector `ceil/floor/trunc/
nearest` to **libcalls** (`X86Pshufb`, `CeilF32`, … one per lane), and everything else to SSE2
sequences. V8 enables wasm SIMD on x64 only with SSE4.1. For us: Wasmtime runs SIMD on every
x86-64, so we must too (D25 parity: the oracle accepts these modules), but its libcall route is
closed to us (D25: no computation helpers). Every operator gets a native SSE2 sequence.

Operators by SSE2 cost (all 236 classified; table in `frontend/simd.py`'s comments later):

| class | ops | examples |
|---|---|---|
| D: 1–3 instructions | 158 | `paddb..q`, `psub*`, saturating `padds/paddus`, `pavgb/w`, `pminub/pmaxub`, `pminsw/pmaxsw`, `pcmpeqb/w/d`, `pcmpgtb/w/d`, `pmullw`, `pmaddwd` (dot), 16/32/64-bit shifts (count masked in a GPR, `movd`), `packsswb/packuswb/packssdw`, `pmovmskb`/`movmskps/pd`, `punpck*` (zero-extend), `pextrw/pinsrw`, `cvtdq2ps/cvtdq2pd/cvtps2pd/cvtpd2ps`, packed float arithmetic, `cmpps/cmppd`, `pmin`/`pmax` (= `minps`/`maxps` with swapped operands) |
| S: 4–12 instructions | 68 | 8-bit shifts (16-bit shift + mask), `i64x2.shr_s`, unsigned compares (sign bias), `i64x2` compares, `i8x16`/`i32x4` signed and `i16x8`/`i32x4` unsigned `min`/`max` (compare + blend), `i32x4.mul` and `i64x2.mul` (`pmuludq`), `abs`, sign-extending loads and extends of i32, `extmul`, `extadd_pairwise`, `i16x8.narrow_i32x4_u`, `q15mulr_sat_s` (no `pmulhrsw`), `i8x16.popcnt` (SWAR), float `min`/`max` with wasm's NaN and ±0 rules, unsigned converts, `trunc_sat`, lanes without `pinsrd/pinsrb` |
| L: no SSE2 form | 10 | `i8x16.shuffle`, `i8x16.swizzle`, `f32x4`/`f64x2` `ceil floor trunc nearest` |

L lowering: `shuffle` first matches immediates that one SSE2 instruction does (`pshufd`,
`pshuflw/hw`, `punpckl/h*`, `shufps`, `psrldq/pslldq` + `por`); otherwise a straight-line gather of 16
bytes from the operand slots into the scratch slots (`movzx` + `mov`, ~33 instructions, no table).
`swizzle`: per lane, index byte, out-of-range mask (`cmp`; `sbb`), masked byte load from the
operand's slots, ~6 instructions × 16, flags only inside the method (LESSONS 1). Rounding: f32 lanes
with |x| < 2^23 by `cvttps2dq`/`cvtdq2ps` plus a ±1 fix for floor/ceil and `(|x| + 2^23) − 2^23` for
nearest (ties-to-even from MXCSR's default); f64 lanes by the ±2^52 magic add for every lane
(`cvttpd2dq` reaches only |x| < 2^31); lanes outside the range or NaN keep x; the sign is OR-ed back
(−0, `ceil(−0.5) = −0`); NaN lanes are quieted by `x * 1.0`, as `pyc`'s scalar helpers.

**Measured over `simd/`** (each module disassembled by `wasm-tools print`, the file counted under its
hardest operator): every one of the 236 operators occurs; 18 739 commands are in files whose
operators are all class D, 6 150 in files with S at most (30 files), 1 101 in files with an L
operator (`simd_f32x4_rounding`, `simd_f64x2_rounding`, `simd_lane`, `simd_load`, `simd_splat`).

### JIT, aarch64 (ARMv8.0-A NEON)
Nearly every operator is 1–3 NEON instructions: `tbl` (two-register for `shuffle`; one-register
`swizzle`, whose out-of-range rule is wasm's), `frintp/m/z/n`, `fmin/fmax` (wasm's NaN and ±0 rules
as for scalars, S0004), `fcvtzs/fcvtzu` (saturating, NaN → 0 = `trunc_sat`), `fcvtzs .2d` + `sqxtn`
for `trunc_sat_f64x2_*_zero`, `fcvtn/fcvtl`, `sqrdmulh` (= `q15mulr_sat_s`), `cnt`, `saddlp/uaddlp`,
`smull/smull2/umull/umull2`, `sqxtn/sqxtun(2)`, `uminv/umaxv` (`all_true`/`any_true`), `cmgt/cmge .2d`,
`abs .2d`. Sequences: `i64x2.mul` (no 64-bit lane multiply: `xtn`/`umull`/`shll` form or both lanes
through GPRs), the four `bitmask` ops (`sshr` + `and` with lane weights + `addv`, two halves for
`i8x16`), variable shifts (`dup` of the masked count, `neg` for right shifts, `sshl/ushl`), `pmin/pmax`
(`fcmgt` + `bsl`). FPCR stays the process default (FZ = 0, DN = 0, S0004); AArch64 Advanced SIMD
honours it (no AArch32-style flush-to-zero). Scratch never includes `v8`–`v15`.

### NaN and determinism (D25 parity)
Integer operators, data movement, sign operators and non-NaN float results are deterministic and
must equal Wasmtime bit for bit. A NaN result is only required to be in the spec's set (canonical or
arithmetic, `nans_N`), and Wasmtime does not canonicalise by default
(`cranelift_nan_canonicalization` is off): its NaN bits are the hardware's (x86 produces the
negative "indefinite" `0xFFC00000` for invalid operations; aarch64 with DN = 0 propagates a quieted
input). So, as for scalars: the spec runner checks `nan:canonical`/`nan:arithmetic` per lane (it
does today), differential runs against Wasmtime and between targets compare NaN lanes by class, not
bits (TESTING.md), and neither JIT adds canonicalisation work. `pyc` uses its scalar helpers' rules
(quiet the input NaN, `nan` for `min`/`max`).

### Claiming `simd`
`tests/spec/claims.py` maps the directory `simd` to the feature key `simd`. Until a target claims
it, its runs list `simd: unclaimed (25 990 commands)` and CI runs nothing there. Progress is
measured with an explicit directory argument, which bypasses claims:
`uv run python -m tests.spec.run --engine pyc simd` (failures count as failures; not a CI gate). A
target claims `simd` (adds it to `CLAIMS[target]`, then `python3 tools/features.py` updates
FEATURES.md and features.json) in the step where:

* the explicit run has **at most 25 failing commands** (≈ 0.1 % of 25 481; Q1), each with a reason
  that names a recorded deviation or a fix to come, listed one per line in `xfail/<target>.txt`;
* no whole-file entry (`simd_x.wast:*`), no `unsupported:` and no `wrong kind` line for `simd/`;
* for a JIT: its `simd/` entries equal `pyc`'s (the D25 parity test covers directories both
  claim).

Between the `pyc` claim (5b) and a JIT's (5d, 5e), `auto` keeps selecting the JIT and a SIMD module
fails there with `Unsupported("simd")`, as today (Q2).

## Test plan
* Every step: `uv run pytest -q`; `uv run python -m tests.spec.run --engine {pyc,jit-x86_64}` core
  numbers unchanged; `python3 tools/check_docs.py`; `python3 tools/features.py --check`.
* **Identity**: before/after lists of `tools/jit_dump.py --target {jit-x86_64,jit-aarch64}` equal for
  every module without v128 and CoreMark; for `pyc`, the sha256 of the generated source of every
  core spec module and CoreMark before/after equal.
* **Encodings**: every SSE2 and NEON form of the `simd.py` modules equals `llvm-mc -show-encoding`
  (x86-64 and aarch64 triples, as `test_encoder*.py`).
* **ISA**: `uv run python tools/isa_all.py` and `--target jit-aarch64` pass with the SIMD modules
  compiled; planted `pshufb`, `roundps`, `pminsd`, `pmulld`, `pinsrd` blobs fail. **At run time on an
  SSE2-only CPU** (checked here): `qemu-x86_64 -cpu qemu64,-pni,-ssse3,-sse4.1,-sse4.2,-popcnt` runs
  uv's CPython 3.13.17; inside it `paddq` runs and `pshufb`/`roundps` raise SIGILL. The x86 `jit`
  CI job runs `--engine jit-x86_64 simd` under that CPU.
* **Boundary tests** (`tests/jit/test_simd_abi.py`, `tests/test_pyc_simd.py`): v128 parameters and
  results through the entry, host functions with v128 parameters and results, imported and
  exported v128 globals, `call_indirect`/`return_call` with v128, v128 `select`/`drop`, v128 block
  parameters, a SIMD op under deep recursion (`STACK_OVERFLOW`, scratch slots inside the frame).
* **Differential**: `wasm-tools smith --simd-enabled` modules on each target vs Wasmtime (NaN lanes
  by class), in the differential job when it exists (R3).

### Measurement plan
* CoreMark (`tools/run_coremark_pyc.py`, `tools/run_coremark_jit.py`, both JIT targets) before and
  after 5b and 5c, alternated in one session; identity above makes a change a bug, the row records it.
* **SIMD benchmark**, new: `poc/bench/simd/kernels.c`, freestanding
  `clang --target=wasm32 -O3 -nostdlib [-msimd128]` (clang 18 here builds it: a u8 saturating-add
  loop gave 11 SIMD instructions) — u8 saturating add (image blend), f32 dot product, i8 scan
  (`eq` + `bitmask`), 4×4 f32 matrix multiply, a byte table lookup (`swizzle`), `f32x4.nearest`, each
  with a checksum. Built scalar and SIMD; rows: each target, SIMD vs scalar build, vs Wasmtime.
* Real program for compile time and coverage: `webp_codec.wasm` (playwright-core, 565 369 bytes,
  17 071 SIMD instructions, 102 distinct operators, five Emscripten imports): compile time, D35 lists,
  ISA gate; running it needs its JavaScript glue's imports (not attempted).

## Plan of steps
| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 5a | front end: `frontend/simd.py` table, decoding, validation, `drop`/`select` annotation; targets refuse v128 as `unsupported: simd` | implementer | `uv run python -m tests.spec.run --engine pyc simd`: all 671 `assert_invalid` and every binary module of `simd/` decode and validate (`assert_invalid` pass; others fail only as `unsupported: simd`); core numbers and the identity lists unchanged | table ~300, hooks ~40, tests ~150 |
| 5b | `pyc`: `targets/pyc/simd.py`, all 236 operators | implementer | explicit `simd` run ≤ 25 failures → `pyc` claims `simd`; `pyc` source identity; CoreMark `pyc` row; BENCHMARKS row for the representation micro-benchmark | ~700, tests ~200 |
| 5c | JIT core and amendment (both backends): slot map, `jit/common/simd.py`, `Layout` widths, boundary split, the `simd` method with data movement only (`v128.const`, `load`/`store` incl. lanes, splat, `extract`/`replace_lane`, bitwise, `select`, globals) on x86-64 and aarch64 | jit-engineer, plan approved first | identity lists for both targets; `tests/jit/test_simd_abi.py` on x86-64 and under QEMU aarch64; `simd_load`, `simd_store`, `simd_lane`, `simd_splat`, `simd_select`, `simd_linking` equal `pyc` | core ~150, common ~200, each backend ~250 |
| 5d | `jit/x86_64/simd.py`: every operator at SSE2, encodings vs `llvm-mc`, ISA allowlist, SSE2-only QEMU job | jit-engineer | `--engine jit-x86_64 simd` = `pyc`'s failures → claim; `spec_diff pyc jit-x86_64` empty for `simd/`; run under `qemu64` without SSSE3/SSE4.1; SIMD benchmark rows | ~1 100, tests ~300 |
| 5e | `jit/aarch64/simd.py`: every operator in NEON | jit-engineer | same on the arm64 runners and under QEMU (BTI); D35 lists equal both ways | ~700, tests ~250 |
| 5f | review of 5c–5e by a `reviewer` with no access to the implementers' reasoning; D39 amendment recorded; this spec `implemented` | reviewer | findings closed; CoreMark rows unchanged | docs |

A step that needs more than its size stops and reports (CLAUDE.md). 5c changes the frozen
interface: both backends in that step (D39); 5d and 5e add operators only.

## Alternatives considered
* **A vector register class now** (a vector `tos` like the integer one, or pinned v128 locals):
  fewer loads/stores per operator, but every method must then preserve it, floats would want the
  same class (not byte-identical any more), and a pinned class across calls needs the Win64 entry to
  save `xmm6`–`xmm15` (160 bytes) and the AAPCS64 entry `d8`–`d15` (64 bytes). Without a measurement
  it is an optimisation before need. A vector `tos` (one caller-saved register: `xmm5`/`v7`, flushed
  before calls and edges like the integer `tos`) is the candidate if the SIMD benchmark shows the
  slot round trips cost (Q3).
* **Require SSE4.1/SSSE3 for SIMD** (V8's choice): a SIMD module would declare x86-64-v2 and be
  refused on v1 CPUs, against D24's baseline rule; Wasmtime runs it there. An optional v2 variant
  (`pshufb`, `roundps`, `pminsd`, `pmulld`, `pinsr*`) later under D33 (Q4).
* **Cranelift's SSE2 libcalls** for `pshufb` and rounding: a computation helper, excluded by D25.
* **v128 as one 16-byte slot, or slots of 16 bytes everywhere**: every frame and the documented
  two-slot ABI change; non-SIMD code would not stay identical.
* **A separate v128 area in the frame**: the callee's parameters would not be at `frame[0..n)` as
  ARTIFACT.md states; calls would need copies.
* **`pyc` v128 as `bytes`, a `bytearray` (pywasm) or a tuple of lanes**: measured above; `bytes` is
  faster only for lane arithmetic through `struct` (798 vs 1 061 ns) and 4× slower for bitwise ops;
  tuples need a shape per value while v128 is untyped.
* **Inline SIMD in the existing files**: excluded by the maintainer (requirement 1).
* **Copy-and-patch stencils for SIMD** (R3: "SIMD cheaper"): belongs to that separate decision.

## Open questions
* **Q1 Claim threshold.** (a) 0 failures; (b) ≤ 25 listed failures, no whole-file, `unsupported:` or
  `wrong kind` entries; (c) ≥ 99 % (≤ 254). Recommended: (b).
* **Q2 `auto` between the `pyc` claim and the JIT claims.** (a) `pyc` claims at 5b; `auto` keeps the
  JIT, SIMD modules fail there with `Unsupported("simd")` until 5d/5e (as today; D25's "a feature is
  complete when it passes on both" then holds at 5e); (b) `pyc`'s claim waits for 5d so the claims
  never differ; (c) mark the JITs `complete = False` meanwhile, which sends every user to `pyc`.
  Recommended: (a), with 5c–5e directly after 5b.
* **Q3 Register class.** (a) none: slots plus scratch, as specified; (b) a vector `tos` now; (c)
  pinned vector locals. Recommended: (a); (b) only with a SIMD benchmark measurement.
* **Q4 x86-64-v2 SIMD variant** (SSSE3/SSE4.1 forms, D33 opt-in). (a) never; (b) after 5d, only for
  operators where the benchmark shows a gain; (c) inside 5d. Recommended: (b).
* **Q5 NaN canonicalisation option** (Wasmtime's `cranelift_nan_canonicalization`). (a) not now;
  (b) in 5d/5e. Recommended: (a); revisit with the `Config` work of phase 6.
* **Q6 Opcode numbers in the shared tuples.** (a) `0xFD000 + sub`; (b) `0xFD00 + sub` (collides once
  sub ≥ 0x100: relaxed SIMD); (c) a `("simd", sub)` tuple head. Recommended: (a).
* **Q7 Relaxed SIMD.** (a) a follow-up spec with Wasmtime's deterministic lowering; (b) inside 5d/5e.
  Recommended: (a).
* **Q8 Shared modules** `frontend/simd.py` and `jit/common/simd.py`, beyond the three named. (a)
  accept (one operator table for reader, validator and targets; the core's SIMD stack effects out
  of `compiler.py`); (b) inline into `binary.py`/`validate.py`/`compiler.py`. Recommended: (a).

## Changes
* 2026-11-01: created (draft).
