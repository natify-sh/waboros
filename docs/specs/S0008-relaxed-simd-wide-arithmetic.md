---
title: Relaxed SIMD (deterministic profile; madd native per target) and wide arithmetic on pyc, jit-x86_64 and jit-aarch64
kind: spec
id: S0008
status: implemented
superseded_by:
related: [D19, D22, D24, D25, D35, D39, D41, D42, D47, D48, S0001, S0005, R3]
owner: maintainer
updated: 2026-11-04
---

# S0008 Relaxed SIMD (deterministic profile; madd native per target) and wide arithmetic on pyc, jit-x86_64 and jit-aarch64

## Summary
Two small proposals that Wasmtime 50 enables by default ([WASM-PROPOSALS.md](../WASM-PROPOSALS.md)),
done as one parity step on the three targets (ROADMAP "Next" item 2; memory64, exceptions and GC follow):

1. **Relaxed SIMD** (Wasm 3.0; 20 operators, `0xFD` sub-opcodes 256–275; `relaxed-simd/`, 79
   commands). **Decided (maintainer, 2026-11-04): the deterministic profile of Wasm 3.0 for 16
   operators on every target; `relaxed_madd`/`relaxed_nmadd` native per target, as Wasmtime**
   ([ISA-LEVELS.md](../ISA-LEVELS.md), last bullet; Q5). The profile fixes every relaxed
   parameter to 0 (spec, exec/numerics, "Relaxed Operations": "In the deterministic profile, every
   parameter is prescribed to be 0"; `relaxed(R)[A0, …, An] = A0`). Then 14 operators equal existing
   S0005 operators and 6 are short compositions of existing ones. **The front end decodes all 20 as
   operations of their own; each target maps every one of them itself** (Q1, decided): no front-end
   aliasing, so a target can later lower a relaxed operator natively. No new ISA, no level above the
   baseline. The result is the same on every target except madd/nmadd: fused on `jit-aarch64`
   (`fmla`/`fmls`), unfused on `jit-x86_64` (no FMA at the baseline) and on `pyc`. It never depends
   on the host CPU.
2. **Wide arithmetic** (phase-4 proposal, not in 3.0; `i64.add128`, `i64.sub128`, `i64.mul_wide_s`,
   `i64.mul_wide_u`, `0xFC` 19–22; two `i64` results each). On the JITs it needs one new backend
   method, `wide`, that only these four opcodes reach.

This spec fixes the operator mapping, the front-end encoding, the `wide` method, how the tests are
pinned and claimed, and the steps. Status before this step (measured 2026-11-04): explicit
`relaxed-simd` run 0 pass / 79 fail on `pyc` and `jit-x86_64` (every module refused); a
wide-arithmetic module is refused as **malformed** (`binary.py` rejects `0xFC` sub ≥ 18), the
wrong kind, because Wasmtime accepts it.

## Goals / Non-goals
* Goals: `relaxed-simd/` (79 commands) and the pinned wide-arithmetic file (109 commands) pass on
  `pyc`, `jit-x86_64` and `jit-aarch64` and are claimed by each (D25: the same results as `pyc`,
  except madd/nmadd on `jit-aarch64`, see "Semantics");
  JIT code stays at x86-64-v1 (SSE2) and ARMv8.0-A (D24); the D35 lists of every module without these
  opcodes and CoreMark stay byte-identical on both JITs; the generated `pyc` source of every
  module without them stays identical.
* Non-goals: a "native" relaxed mode beyond aarch64 madd/nmadd (x86-64 FMA from x86-64-v3,
  `pshufb` swizzle semantics, hardware dot products), which belongs to the ISA-level work
  ([ISA-LEVELS.md](../ISA-LEVELS.md), D42); a `Config` switch for it;
  other phase-4 arithmetic ideas from the wide-arithmetic overview (`add_overflow`, `add_with_carry`);
  `mpy` (tier 3, D22); the public API; any speed claim (none is made, so no measurement plan).

## Interface

### Front end (`frontend/binary.py`, `frontend/simd.py`, `frontend/validate.py`)
**Relaxed SIMD.** `simd.decode` stops raising `NotImplementedError` for sub-opcodes 256–275:
* **all 20** become new entries of `OPS` (sub-opcode, name, `NONE`, types) and decode to
  `(0xFD000 + sub,)`, `0xFD100`–`0xFD113`, with the types of the table below: `(V, V) → V` for
  swizzle, min/max, q15mulr and the two `relaxed_dot_i8x16_i7x16_s`; `(V) → V` for the four trunc;
  `(V, V, V) → V` for the four laneselect, the four madd/nmadd and `dot_..._add_s`. The decoder
  and the validator do not know the deterministic profile; they see 20 operators.
* **No aliasing in the front end** (Q1, decided: the maintainer's reason is in Alternatives): the
  14 operators that equal an S0005 operator are mapped to its lowering by each target, not rewritten
  to it. A relaxed operator stays visible to every target as relaxed.

**Wide arithmetic.** `binary.py`, `0xFC` branch: sub 19–22 decode to `(0xFC13,)`, `(0xFC14,)`,
`(0xFC15,)`, `(0xFC16,)` (no immediates; numbers from the proposal's Overview, "Binary Format":
`0xFC 19:u32 ⇒ i64.add128`, 20 `sub128`, 21 `mul_wide_s`, 22 `mul_wide_u`). Sub 18 (reserved for
`memory.discard`) and ≥ 23 stay `Malformed`. `validate.py`: `0xFC13`/`0xFC14` pop four `i64`,
`0xFC15`/`0xFC16` pop two, each pushes `i64`, `i64` (a small table next to `OPSIG`, which holds one
result per opcode). No other front-end change.

### The relaxed operators (opcode → result → lowering on each target)
Each target owns its 20 mappings (`pyc`: its code generator; each JIT: its own table, `PROG` entries
that may point at the same program as the equivalent operator); there is no shared default table.
Spec references are to the Wasm 3.0 spec, exec/numerics, section "Relaxed Operations" (read
2026-11-04); "alt 0" is the first argument of `relaxed(R)[…]`.

| sub | operator | alt 0 (spec text); madd/nmadd: the unfused alternative | each target lowers it as | pyc | x86-64 (SSE2) | aarch64 (NEON) |
|---|---|---|---|---|---|---|
| 0x100 | `i8x16.relaxed_swizzle` | `ivrelaxed_swizzle_lane`: `j < 16` → `i[j]`; `signed(j) < 0` → 0; else `relaxed[0, i[j mod n]]` → 0; note: "behaves like regular ivswizzle" | `i8x16.swizzle` (0x0E) | existing | existing (byte gather, class L) | existing (`tbl`) |
| 0x101–0x104 | `i32x4.relaxed_trunc_{f32x4_s,f32x4_u,f64x2_s_zero,f64x2_u_zero}` | `relaxed[trunc_sat_{s,u}(z), R]`; note: "behaves like regular trunc_sat" | 0xF8, 0xF9, 0xFC, 0xFD | existing | existing | existing |
| 0x109–0x10C | `{i8x16,i16x8,i32x4,i64x2}.relaxed_laneselect` | `ibitselect(i1, i2, relaxed[i3, ext_s(i3 >> N−1)])` → mask `i3`; note: "behaves like ibitselect" | `v128.bitselect` (0x52), same operand order | existing | existing | existing |
| 0x10D, 0x10E | `f32x4.relaxed_{min,max}` | `relaxed[fmin/fmax(z1, z2), …]`; note: "behaves like regular fmin/fmax" | 0xE8, 0xE9 | existing | existing | existing |
| 0x10F, 0x110 | `f64x2.relaxed_{min,max}` | as above | 0xF4, 0xF5 | existing | existing | existing |
| 0x111 | `i16x8.relaxed_q15mulr_s` | both `−2^(N−1)`: `relaxed[2^(N−1)−1, …]`; else `iq15mulrsat_s`; note: "behaves like regular iq15mulrsat_s" | 0x82 | existing | existing | existing (`sqrdmulh`) |
| 0x105 | `f32x4.relaxed_madd` | `relaxed[fadd(fmul(z1, z2), z3), fma(z1, z2, z3)]`; note: "the unfused behaviour is used" | `f32x4.add(f32x4.mul(a, b), c)` | the two helpers composed | `mulps 0 1; addps 0 2` | fused: `fmla.4s 2 0 1` (`c + a·b`, result moved to v0) |
| 0x106 | `f32x4.relaxed_nmadd` | `frelaxed_madd(−z1, z2, z3)` | `add(mul(neg a, b), c)` ≡ `c − a·b` (below) | composed | `mulps 0 1; subps 2 0` | fused: `fmls.4s 2 0 1` (`c − a·b`, result moved to v0) |
| 0x107, 0x108 | `f64x2.relaxed_madd`, `_nmadd` | as f32x4 | as f32x4 | composed | `mulpd`, `addpd`/`subpd` | `.2d` forms (fused) |
| 0x112 | `i16x8.relaxed_dot_i8x16_i7x16_s` | `vextbinop` row `relaxed_dot_s`: `sx2 = relaxed[s, u]` → s; `f = ivrelaxed_dot = relaxed[ivdot, ivdotsat]` → `ivdot_16` (wrapping) | Σ of products of sign-extended byte pairs, mod 2^16 | `i16x8.add(mul(ev a, ev b), mul(od a, od b))`, `ev`/`od` by `shl`/`shr_s` 8 | 11: `movaps 2 0; psrawi 2 8; movaps 3 1; psrawi 3 8; pmullw 2 3; psllwi 0 8; psrawi 0 8; psllwi 1 8; psrawi 1 8; pmullw 0 1; paddw 0 2` | `smull.8h 2 0 1; smull2.8h 3 0 1; addp.8h 0 2 3` |
| 0x113 | `i32x4.relaxed_dot_i8x16_i7x16_add_s` | `vextternop`: `N = relaxed[2·N1, N2]` → 16: `ivrelaxed_dot_16`, extend_s to 32, `ivadd_pairwise_32`, `add c3` | `i32x4.add(i32x4.extadd_pairwise_i16x8_s(dot16(a, b)), c)` | composed | the dot above with temporaries in xmm3/xmm4 (xmm2 holds `c`), then `pcmpeqw 1 1; psrlwi 1 15; pmaddwd 0 1; paddd 0 2` (~15) | the dot, then `saddlp.4s 0 0; add.4s 0 0 2` |

Notes on the table:
* **nmadd**: `fmul(−a, b) = −fmul(a, b)` for non-NaN operands (round-to-nearest is sign-symmetric),
  and `(−p) + c = c + (−p) = c − p` (IEEE 754 defines subtraction that way; addition is commutative,
  signed zeros included). So `c − a·b` gives the same bits as the spec's form except for the sign
  and payload of a NaN result, which the spec leaves open and our comparisons take by class (S0005,
  "NaN and determinism"). Each target may use either form.
* **Fused only on aarch64.** The unfused result is two roundings. `jit-x86_64` (SSE2 has no FMA)
  and `pyc` use it; `jit-aarch64` uses the fused `fmla`/`fmls` (one rounding), as Wasmtime does on
  aarch64. `fmls Vd, Vn, Vm` computes `Vd − Vn·Vm` fused (ARM ARM, C7.2 "FMLS (vector)", to be
  checked against the manual and `llvm-mc` in step 2: not available when this was written), so
  nmadd = `fma(−a, b, c)` = `fmls Vd(c), Va, Vb`; madd = `fmla Vd(c), Va, Vb`. The accumulator is
  `c`, so the program copies `c` into a scratch register, fuses, and moves the result to v0.
  `pyc` composes its per-lane helpers, which round the product to the lane's precision before the
  add. Computing `a*b + c` in doubles and rounding once to f32 would be wrong for the unfused
  targets: `FLT_MAX · 2 − FLT_MAX` gives `FLT_MAX` (the fused answer) instead of `+inf`. The pinned
  test accepts both answers, so only the Test plan's differential catches this.
* **Native lowering later.** Because the opcodes reach the targets, a target may replace a mapping
  when its ISA level allows, without a front-end change: x86 `cvttps2dq` for relaxed_trunc,
  `minps`/`maxps` for relaxed_min/max, `pshufb` for relaxed_swizzle (SSSE3). Not in this spec.
* **dot**: the products of two sign-extended bytes fit in 16 bits (from −16 256 to 16 384), so
  `pmullw`/`smull` are exact; the pairwise add wraps (`paddw`, `addp`), as `ivdot` does (`−128·−128 ·
  2 = 32 768` wraps to `−32 768`; a pinned command checks this lane).
* New encodings: x86 none (every form is in `FORMS`/`SHIFTS` already, all SSE2); aarch64
  `addp .8h` and `smull/smull2 .8h` if not yet in `FORMS`, each checked against `llvm-mc`.
  `tools/isa_check.py` needs no new mnemonic.

**Verified before writing (2026-11-04, scratch script, not committed):** the table's compositions, built
from `pyc`'s existing helpers, pass all 71 `assert_return` of the pinned `relaxed-simd/` (the 8
other commands are modules), and equal Wasmtime 50.0.1 with `wasm_relaxed_simd_deterministic`
for every operator except madd/nmadd (unfused, as `pyc` and `jit-x86_64` do) on 3 000 inputs each (random, special floats, byte
patterns), NaN lanes by class. For madd/nmadd see "Semantics".

### Wide arithmetic: one backend method (D39)
```python
def wide(self, op: str, dst: Slot, args: tuple[Operand, ...]) -> None: ...
# op: "add128" | "sub128" (args a_lo, a_hi, b_lo, b_hi) | "mul_wide_s" | "mul_wide_u" (args x, y),
# wasm order; the low 64 bits of the result into slot dst.k, the high 64 bits into dst.k + 1
```
Contract (in the style of D39): operands are `Imm` (unsigned 64-bit), `Reg` (`tos` or a pinned pool
register) or `Slot`; `dst.k` is the stack position of the first operand, so the result slots may be
operand slots, and an operand is read before its slot is written. The method may write `tos`
(x86-64 `mul` needs `rax`): the core calls `need_tos()` first. It preserves the pool, frame, context
and memory-0 registers, leaves no flags live, uses 64-bit forms only, and needs no trap, so it takes
no frame `f`.

Under D39's own rule ("a method only a new opcode reaches is not a change of the interface") this
is **not an amendment**; it is still added to both backends in one step, recorded in the D39
paragraph of DECISIONS.md like the earlier additions, and read by a `reviewer` (Q2). `supported`
gains `0xFC13`–`0xFC16` on both backends.

**Core (`jit/common/compiler.py`)**: one branch for the four opcodes, as `jit/common/simd.py`'s `op`
does for a v128 result. It pops the operands with `fn.pop()` (an `AddK` entry is computed first,
LESSONS 32), calls `need_tos()`, then `b.wide(op, Slot(fn.L + h), args)` (`h` = height after the
pops), then pushes two `SLOTE` entries. The results take at most the operands' slots, so
`frame_slots` does not grow. **Precedent**: a SIMD operator already writes two slots in one backend
call (`_push2`); unlike a v128, the two entries are independent `i64` values afterwards, so no
annotation and no slot-map change. Multi-value calls and blocks already move several results
through slots.

**`pyc` (`targets/pyc/codegen.py`, `runtime.py`)**: four runtime helpers returning `(lo, hi)`,
emitted as `t1, t2 = _add128(a, b, c, d)` and pushed as two `i64` temporaries. That is the shape
multi-value calls use today (`ts = [s.newtmp(ty) …]`, `s.emit("t1, t2 = …")`, `E(t, I64, M64,
k=True)`). Before the statement, the entries below the operands that can trap are materialised
(`flush` with the `x` predicate: LESSONS 28, 30). The helper has no memory or global effect, so
`m`/`g` entries stay lazy.

### Tests: pins, claims, oracle
* **Relaxed SIMD** is in the pinned spec suite (`relaxed-simd/`, 7 files, 79 commands: 71
  `assert_return`, 33 of them `either`, and 8 modules). `tests/spec/run.py` supports `either` already
  (`matches`, `t == "either"`: any alternative, NaN patterns per lane; `test_runner.py::test_either`).
  `i32x4_relaxed_trunc.wast` holds only its module at this pin (no assertion), so the relaxed trunc operators
  get unit and differential tests instead (Test plan).
* **Wide arithmetic** is **not** in the pinned spec suite (`WebAssembly/spec` main, 2026-10-28: no
  file). Source: the proposal repository, `WebAssembly/wide-arithmetic`,
  `test/core/wide-arithmetic.wast` at commit `024d4ce1b28cee7fd006d9c5175e3b7ece52b429` (HEAD on
  2026-11-04), sha256 `9a3353e12e1fb78e8a7e30b7c5b356bfc897c093b83cbfc6a544660b88adcdc1`.
  wasm-tools 1.262.0 converts it: 109 commands (99 `assert_return`, 8 `assert_invalid`, 2
  modules). Wasmtime keeps a near-copy (`tests/misc_testsuite/wide-arithmetic.wast`, 99
  `assert_return`), but the proposal's file is the normative one. `fetch.py` gains a second pin
  (`PROPOSAL_FILES`: directory, URL at the commit, sha256), downloads the one file by `urllib` into
  `wast/wide-arithmetic/` before conversion, refuses a hash mismatch, and records it in the manifest
  (Q3). `counts.json` gains `"wide-arithmetic/wide-arithmetic": 109`. When the proposal reaches the spec
  repository, the pin moves there and this entry goes (S0001 Changes).
* **Claims** (`tests/spec/claims.py`): `DIR_FEATURE["wide-arithmetic"] = "wideArithmetic"`;
  `relaxedSimd` and `wideArithmetic` join `CLAIMS` of each target in the step where its explicit run
  has 0 failures and its results equal `pyc`'s (`spec_diff` empty without `--allow-unsupported`).
  Wasmtime claims `wideArithmetic` too. `tools/features.py`: `"wideArithmetic": ["wide-arithmetic/"]`.
* **Oracle Config** (`wasmtime_engine.py`, S0001: "every switch the claimed directories need,
  explicit"): add `wasm_wide_arithmetic` (on by default in 50.0.1, explicit by the rule) and
  `wasm_relaxed_simd_deterministic` (Q4). Measured with both switches: the oracle passes all 71 + 99
  `assert_return` and 8 `assert_invalid`. Madd/nmadd are exempt from the bit-exact oracle
  comparison on x86 (Q4).

### Context ABI, artifact, ISA: unchanged
No new context word, trap code, libcall, trampoline or frame rule; baseline instructions only
(SSE2: `adc`, `sbb`, `mul`, `imul` are already in the x86-64-v1 allowlist; ARMv8.0-A: `adds`, `adc`,
`subs`, `sbc`, `mul`, `umulh`, `smulh`). `ctx_abi_version`, the artifact format and `isa_level` do not
change (docs/ARTIFACT.md needs nothing). No per-OS code (D47: no rows touched). riscv64 (D48) will
implement `wide` without flags (`add`, `sltu` for the carry): one reason the method takes whole
operations instead of exposing a carry.

## Semantics

### Relaxed SIMD: the deterministic profile, madd/nmadd native per target
Sixteen operators give alt 0 on every target and in `pyc`, independent of the host CPU, so `pyc`
stays a bit-exact oracle for them (NaN payloads aside, as for all float operators, S0005).
`f32x4/f64x2.relaxed_madd` and `relaxed_nmadd` follow Wasmtime's native choice per architecture
(maintainer, 2026-11-04): **fused on `jit-aarch64`, unfused on `jit-x86_64` and `pyc`.** So relaxed
SIMD is **no longer one result on every target**. The pinned tests accept both: in
`relaxed_madd_nmadd.wast` 8 of 18 `assert_return` are `either` with the fused and the unfused value
(first command: `FLT_MAX·2+(−FLT_MAX)` → `FLT_MAX` or `+inf`), the other 10 have one result in both
(checked in the pinned JSON, 2026-11-04; the runner's `either` support is in "Tests"). The code for a target is the same on every host, so D35 (cross-host byte
identity) is unaffected: the result differs by target, not by host. FMA on x86-64 arrives with
x86-64-v3 (FMA3), when ISA levels above the baseline are adopted
([ISA-LEVELS.md](../ISA-LEVELS.md)); until then `jit-x86_64` matches Wasmtime on a CPU without FMA.

**Measured deviation from Wasmtime (2026-11-04, Wasmtime 50.0.1, Xeon 2.1 GHz with FMA).**
* Wasmtime's `wasm_relaxed_simd_deterministic` **fuses** `relaxed_madd`/`relaxed_nmadd`:
  `f64x2.relaxed_madd` equals `math.fma` lane by lane on 3 000 of 3 000 inputs. On the same inputs it
  differs from the 3.0 profile's unfused result in 116 (f32x4 madd), 115 (f32x4 nmadd), 30 and 34
  (f64x2) calls. On the other 16 operators it equals the 3.0 profile (0 differences).
* Without the switch (the oracle's current Config), Wasmtime takes the host's native choices:
  23 928 of 60 000 calls differ from the 3.0 profile (swizzle, trunc, laneselect, min/max, dot,
  madd).

So the maintainer's decision is **Wasmtime's native choice per architecture** for madd/nmadd, not
its deterministic switch (which fuses on every host). `jit-aarch64` is fused as Wasmtime is on
aarch64; `jit-x86_64` is unfused as Wasmtime is on an x86 CPU without FMA; `pyc` is unfused (the 3.0
deterministic profile, target-neutral; `math.fma` exists only from Python 3.13, D19: minimum 3.11).
The decision is recorded as D49 (DECISIONS.md). A software FMA on SSE2 was
rejected: a long exact-product sequence per lane, and D25 forbids a computation helper.

**Consequences.**
* Bit-exact comparisons against `pyc` (`spec_diff`, the differential tests, the D25 parity claim)
  exempt madd/nmadd on `jit-aarch64`. Instead its madd/nmadd is checked (Test plan) against a fused
  reference (`math.fma` where Python is 3.13 or newer, exact `Fraction` arithmetic rounded once
  otherwise) and against Wasmtime on an aarch64 host (CI's arm64 job, the platforms Linux arm cell).
* On `jit-x86_64` and `pyc` madd/nmadd are checked against the unfused reference.
* Q4: the oracle switch fuses madd/nmadd on every host, so on x86 hosts it differs from
  `jit-x86_64` for these two operators only.

### Wide arithmetic
From the proposal's Overview, "Execution": `add128`/`sub128` join `c1:c2` and `c3:c4` (low:high)
into 128-bit values and push the low, then the high 64 bits of the sum or difference mod 2^128.
`mul_wide_s` sign-extends both operands to 128 bits, `mul_wide_u` zero-extends them; each pushes the
low, then the high 64 bits of the product. No traps.

**pyc.** `_add128: t = (a | b << 64) + (c | d << 64)`; `_sub128` the same with `-` and `& M128`;
`_mul_wide_u: t = a * b`; `_mul_wide_s` on `a − (a >> 63 << 64)` and the same for `b`, then
`& M128`; each returns `t & M64, t >> 64 & M64`. Operands as `pyc` passes `i64` (`s.n(a)`,
unsigned).

**x86-64** (`rax` = `tos`; `rcx`, `rdx` scratch; the pool never holds rax/rcx/rdx):
* `add128` / `sub128`: `mov rcx, a_lo`; `add|sub rcx, b_lo`; `mov [k], rcx`; `mov rcx, a_hi`;
  `adc|sbb rcx, b_hi`; `mov [k+1], rcx`. Writing slot k early is safe: it holds only `a_lo`, already
  read. `tos` is not written. An operand is a register, a slot or an imm32. A 64-bit immediate outside
  imm32 goes into `rdx` by `mov rdx, imm64` just before its use.
* **Flags (LESSONS 1)**: between `add`/`sub` and `adc`/`sbb` only `mov` is emitted. Constants are
  loaded by an explicit `mov` (`mov ecx, 0` for zero), never through the backend's `move`, which may
  emit `xor` for zero and clear CF.
* `mul_wide_u` / `_s`: `move(rcx, y)`, then `move(rax, x)`. That is divrem's order: `y` may be in
  `tos` = rax, so it is read first (LESSONS 32). Then `mul rcx` / `imul rcx` (`F7 /4`, `F7 /5`: the
  one-operand forms write rdx:rax; the encoder's `UNARY` gains `imul`), `mov [k], rax`,
  `mov [k+1], rdx`. `rax` is clobbered, which is why the core calls `need_tos()`.
* **Zero-extension (LESSONS 5)**: 64-bit forms only (`w=True`); a 32-bit form would clear the upper
  half of a result.

**aarch64** (`x0` = `tos`; x9–x15 scratch): operands into registers first (`_r`, x9–x12; `ldr`,
`movz`/`movk` leave the flags), then `adds x13, a_lo, b_lo; adc x14, a_hi, b_hi` or `subs`/`sbc`
(after `subs`, C = NOT borrow, which `sbc` expects); `mul x13, x, y` plus `umulh` or `smulh x14, x, y`;
then `str x13, [k]`, `str x14, [k+1]`. `tos` is not written. `adds`/`subs` join `cmp`/`cmn`/`ccmp`/`fcmp`
as the only flag writers, each consumed by the next instruction (the backend docstring says so).
New encoder forms: `adc`, `sbc`, `umulh`, `smulh`, each checked against `llvm-mc`.

## Test plan
* Every step: `uv run pytest -q`; `uv run python -m tests.spec.run --engine pyc` and
  `--engine jit-x86_64`: core numbers unchanged (54 385 pass, 91 expected failures before this
  step) plus the newly claimed directories; `python3 tools/check_docs.py`;
  `python3 tools/features.py --check`.
* **Identity**: `uv run python tools/jit_dump.py --target {jit-x86_64,jit-aarch64}` before and after.
  Only lines of `json/relaxed-simd/` and `json/wide-arithmetic/` modules may change or appear.
  CoreMark's line is unchanged. The sha256 of `pyc`'s generated source of every core spec module and
  CoreMark is unchanged.
* **Spec parity, both bounds modes**: for each JIT, `uv run python -m tests.spec.run --engine <jit>
  relaxed-simd wide-arithmetic` with `WABOROS_JIT_BOUNDS=guard` and `=explicit`: 79 + 109 pass, 0 fail.
  `uv run python tools/spec_diff.py pyc <jit> relaxed-simd wide-arithmetic` is empty (no
  `--allow-unsupported` at the claim); for `jit-aarch64` the madd/nmadd results are exempt from the
  comparison (fused there), and its pinned `either` assertions pass. `jit-aarch64` under QEMU:
  `python3 tools/run_arm64.py -m tests.spec.run --engine jit-aarch64 --in-process relaxed-simd
  wide-arithmetic`, in both modes. `jit-x86_64` `relaxed-simd` also under the SSE2-only `qemu64`
  CPU with `WABOROS_JIT_BOUNDS=explicit`, as the CI `simd` step does.
* **ISA**: `uv run python tools/isa_all.py` and `--target jit-aarch64` pass with the new modules
  compiled.
* **Encodings**: the new x86 form (`imul r/m64`, `F7 /5`) and aarch64 forms (`adc`, `sbc`, `umulh`,
  `smulh`, `addp .8h`, any new `smull` form) equal `llvm-mc -show-encoding` (`test_encoder*.py`).
* **Unit tests, wide** (`tests/jit/test_wide.py`, both backends; aarch64 under QEMU): every
  operand kind in every position: `Imm` 0, a small imm, an imm above imm32, a pinned `Reg`, `tos`
  (the last operand computed), a `Slot`, a local slot. Edge values: carry and borrow across 64 bits,
  `2^64 − 1 + 1`, `0 − 1`, `INT64_MIN · INT64_MIN`, `−1 · −1` signed and unsigned. Each result is
  compared with `pyc` and with Wasmtime. A planted `xor ecx, ecx` between `add` and `adc` must fail one
  case (the flags test).
* **Differential, relaxed** (a test, seeded, ~500 inputs per operator: random, special floats,
  byte patterns as in the scratch check above): for each target, the 16 operators other than
  madd/nmadd equal Wasmtime with `wasm_relaxed_simd_deterministic`. madd/nmadd: on `pyc`
  and `jit-x86_64`, equal to the unfused reference bit for bit and **differ from fused** on the
  `FLT_MAX` case; on `jit-aarch64`, equal to a fused reference (`math.fma` where Python >= 3.13,
  exact `Fraction` arithmetic rounded once otherwise) and **differ from unfused** on the same case,
  and equal to Wasmtime on an aarch64 host in CI's arm64 job (platforms Linux arm cell; under QEMU
  the reference alone). All comparisons take NaN lanes by class. This covers `relaxed_trunc`, which has no pinned assertion.
* **Front end**: decode and validate of each of the 20 relaxed sub-opcodes (each yields its own
  `0xFD100`–`0xFD113` tuple and the types of the table); `0xFC` 18 and 23 stay `Malformed`; wrong operand types fail validation;
  the wide `assert_invalid` commands pass on every target.

## Plan of steps
| step | content | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 1 | front end (20 `OPS` entries, `0xFC` 19–22, validator); `pyc` (maps all 20: 14 to its existing helpers, 6 new compositions; 4 wide helpers, the 0xFC hook); `fetch.py` second pin, `counts.json`, `claims.py`, `features.py`, oracle switches; `pyc` claims `relaxedSimd` and `wideArithmetic` | implementer | `--engine pyc relaxed-simd wide-arithmetic`: 188 pass, 0 fail; `--engine wasmtime` the same; `pyc` source identity; JIT D35 lists identical (modules with relaxed or wide opcodes are `unsupported` on the JITs until steps 2 and 3); front-end and `pyc` tests | ~215 incl. tests |
| 2 | all 20 relaxed operators in `x86_64/simd.py` and `aarch64/simd.py` (14 entries pointing at the existing programs, 6 new compositions, any new NEON forms); both JITs claim `relaxedSimd` | jit-engineer, plan approved first | 79 pass on both JITs, guard and explicit, aarch64 under QEMU, x86 under `qemu64`; `spec_diff` empty; D35: only `json/relaxed-simd/` lines change; ISA gates; the differential test | ~80 + tests ~100 |
| 3 | `wide`: core branch, both backends, encoder forms; both JITs claim `wideArithmetic` | jit-engineer, plan approved first | 109 pass on both JITs, guard and explicit, QEMU; `spec_diff` empty; `test_wide.py`; D35: only `json/wide-arithmetic/` lines appear; ISA gates; encodings vs `llvm-mc` | core ~20, each backend ~35, encoders ~15, tests ~120 |
| 4 | review of steps 2–3 by a `reviewer` without the implementers' reasoning; docs; this spec `implemented` | reviewer, then orchestrator | findings closed; `check_docs`, `features.py --check` | docs |

A step that needs more than its size stops and reports. Docs per step (STANDARDS.md, "Which
document to update"): FEATURES.md through `python3 tools/features.py` plus its notes (relaxed SIMD:
"deterministic profile of 3.0; madd/nmadd native per target as Wasmtime: fused on aarch64,
unfused on x86-64 and pyc"; wide
arithmetic: done); WASM-PROPOSALS.md rows (relaxed SIMD, wide arithmetic → yes on all three);
TARGETS.md (the "not done" list); ROADMAP.md ("Next" item 2 done); TESTING.md (the proposal pin as a
test source); S0001 Changes (second pin, the two oracle switches); DECISIONS.md (the deterministic
profile and madd/nmadd native per target, one new decision, on approval; the `wide` method in D39's paragraph);
ISA-LEVELS.md (relaxed SIMD now done at the baseline; the native-mode bullet stays).

## Alternatives considered
* **Front-end aliasing** (the 14 operators that equal an S0005 operator decode to it): ~40 fewer lines
  in three places, and the 14 identities cannot drift between targets. **Rejected by the maintainer
  (Q1, 2026-11-04)**: aliases erase which instruction was relaxed, and a target must keep the freedom
  to lower a relaxed operator natively when ISA levels are extended (x86 `cvttps2dq` for
  relaxed_trunc, `minps`/`maxps` for relaxed_min/max, `pshufb` for relaxed_swizzle from SSSE3). The
  cost of the chosen design: each target owns 20 mappings, and the deterministic choice for the 16
  could drift between targets; the differential test (Test plan) guards that.
* **A shared default table in the front end or the core** that each target overrides: rejected with
  the same reason, one more layer (Q1: "no shared default table").
* **Composites expanded by the JIT core into several `simd` calls** (madd = `mul` then `add` in
  place): works for madd/nmadd without temporaries, but the dot products need temporaries (scratch
  slots that the operator's own scratch rule overlaps). Programs in the backends' tables are shorter and
  follow S0005's pattern.
* **Unfused madd on every target (the plain 3.0 profile)**: one result everywhere, `pyc` a bit-exact
  oracle for all 20 operators, but it deviates from Wasmtime on aarch64 and wastes `fmla`. Rejected
  by the maintainer's decision (Q5).
* **Fused madd on every target, as Wasmtime's deterministic switch**: needs FMA, which the x86-64
  baseline lacks (software FMA per lane, or `math.fma` that Python 3.11-3.12 lacks). Rejected by the
  maintainer's decision; x86-64 FMA waits for x86-64-v3.
* **Wide arithmetic in the core with existing operations, no backend method**: `add128` is
  `add`, `cmp_set lt_u` for the carry, two more `add`s through `tos` and a scratch slot (~6 backend
  calls). `mul_wide` needs four 32×32 partial products, carries and, for `_s`, the correction
  `hi −= (a < 0 ? b : 0) + (b < 0 ? a : 0)` (~20 calls, ~40 instructions against 4 to 6). Every
  backend, including riscv64 and sljit later, would get it for free. It is more core code (~60 lines,
  temporaries) and gives up the instructions the proposal exists for. Not chosen; Q2 (b).
* **The high half in `tos`** (low to a slot, high to `tos`): saves a store when the next instruction
  consumes the high half. That is an optimisation without a measurement; not now.
* **Vendor `wide-arithmetic.wast`** into `tests/spec/` instead of fetching it: no network at fetch
  time, but it is a second copy of upstream data in the repository, which S0001 avoids; the hash pin
  gives the same reproducibility (Q3).

## Open questions
All decided (maintainer, 2026-11-04).
* **Q1 Where the deterministic choice lives. Decided: (b) in full.** The front end decodes all 20
  relaxed opcodes as their own operations; every target maps each of the 20 itself (to its lowering
  of the equivalent operator, or to the 6 compositions). No front-end aliasing, no shared default table.
* **Q2 Wide arithmetic in the JIT. Decided: (a)** the `wide` method as specified, recorded as a D39
  addition that is not an amendment, with a reviewer pass.
* **Q3 How to pin the wide-arithmetic tests. Decided: (a)** `fetch.py` downloads the one
  `wide-arithmetic.wast` at commit `024d4ce1…` with its sha256; it moves to the spec pin when the
  proposal merges.
* **Q4 The oracle's relaxed switch. Decided: (a)** the oracle gets `wasm_relaxed_simd_deterministic`
  (and `wasm_wide_arithmetic`); madd/nmadd are exempt from the bit-exact oracle comparison on x86
  and compared against the unfused reference instead.
* **Q5 madd/nmadd versus Wasmtime. Decided (maintainer, 2026-11-04):** "as Wasmtime: FMA on aarch64
  and unfused on SSE2; FMA for x86-64 later, when we add higher ISA levels". So `jit-aarch64` is
  fused (`fmla`/`fmls`), `jit-x86_64` and `pyc` unfused; the other 16 operators keep the
  deterministic profile on every target (native results for them come with ISA levels). Recorded as D49.

## Changes
* 2026-11-04: created (draft).
* 2026-11-04: Q5 decided: madd/nmadd native per target as Wasmtime (fused on aarch64, unfused on
  x86-64 and pyc); consequences in Semantics, Test plan, Alternatives, Q4.
* 2026-11-04: approved by the maintainer: Q1 (b) in full, Q2 (a), Q3 (a), Q4 (a); Q5 as recorded. Q1 rewrote
  the Summary, the front-end paragraph, the operator table's lowering column, Plan of steps 1-2, the Test
  plan and Alternatives (front-end aliasing is now a rejected alternative). Recorded as D49.
* 2026-11-04: step 1 done (`pyc`): front end (20 relaxed `OPS`, `0xFC` 19-22, validator), `pyc`
  (20 mappings, 4 wide helpers), second pin, claims; `--engine pyc relaxed-simd wide-arithmetic` and
  `--engine wasmtime`: 188 pass, 0 fail (79 + 109); the claimed `pyc` run 54 573 pass / 91 expected
  failures (54 385 + 188), `jit-x86_64` 54 385 / 91 unchanged; the JITs report these modules `unsupported`
  until steps 2-3.
* 2026-11-04: step 2 done (both JITs): each backend maps the 20 relaxed operators in its own tables
  (x86-64: 9 `PROG` entries reuse the equivalent's program, `relaxed_swizzle` and the 4 laneselect take
  the swizzle and bitselect paths through an opcode-to-kind table `INLINE`; aarch64: 10 `PROG` reuses,
  laneselect through `INLINE`; 6 new programs each, as the operator table). One difference from the
  table: on aarch64 `fmla`/`fmls` leave the result in v2 and the program stores it from there (no move
  to v0). New NEON forms `fmla`/`fmls` `.4s`/`.2d` and `addp .8h` (`smull`/`smull2 .8h` were there);
  x86 none. Both JITs claim `relaxedSimd`: `relaxed-simd` 79 pass / 0 fail in guard and explicit mode on
  `jit-x86_64`, on `jit-aarch64` under QEMU and on `jit-x86_64` under `qemu64` (SSE2 only); the claimed
  `jit-x86_64` run 54 464 pass / 91 expected failures; `spec_diff` empty on both; ISA gates clean; D35:
  only the 8 `json/relaxed-simd/` lines change on either target, in both modes. Differential:
  `tests/jit/test_relaxed_simd.py` (against `pyc`; the comparison with Wasmtime that the Test plan
  asks for was not part of this step and is added in step 4).
* 2026-11-04: step 3 done (both JITs): the core branch for `0xFC13`-`0xFC16` (`WIDEOPS` in
  `common/compiler.py`; each backend adds them to `supported`), `wide` in both backends as specified
  (x86-64: an operand that is not an imm32 goes to rdx by `mov`/`movabs`; aarch64: the operands into
  x9-x12 through `_r`, results through x13/x14). New encoder forms: x86 `imul r/m64` (`F7 /5`,
  `UNARY`), aarch64 `adc`/`sbc` and `umulh`/`smulh` (scalar `Asm` methods; `FORMS` unchanged), each
  equal to `llvm-mc`. Both JITs claim `wideArithmetic`: 109 pass / 0 fail in guard and explicit mode
  on `jit-x86_64` and on `jit-aarch64` under QEMU; the claimed `jit-x86_64` run 54 573 pass / 91
  expected failures (54 464 + 109); `spec_diff pyc jit-x86_64` empty; ISA gates clean; D35: only the
  two compiled `json/wide-arithmetic/` modules change on either target, in both modes.
  `tests/jit/test_wide.py` (both backends, guard and explicit, pinned and unpinned; against `pyc`
  and an exact reference; a planted `xor` between `add` and `adc` fails the carry case). Wasmtime is
  not run by the unit test (the spec file is checked against it in step 1).
* 2026-11-04: implemented: steps 1-3 landed; step 4, the independent review, found no correctness defect
  (9 planted bugs caught by the tests); the Wasmtime comparison of the 16 deterministic operators added
  as a test (`test_deterministic_operators_match_wasmtime`, `pyc` and this machine's JIT, 500 inputs
  per operator, about 1 s); aarch64 madd/nmadd against Wasmtime on a real arm64 host is covered by CI's
  native arm64 run (`arm64` job: `uv run pytest tests/jit` on `ubuntu-24.04-arm` with the dev group,
  which has wasmtime): on `jit-aarch64` the same test also compares madd/nmadd with Wasmtime (whose
  deterministic switch fuses them, as aarch64 does), besides the fused reference.
