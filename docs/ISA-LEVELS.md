---
title: ISA levels above the baseline - how to grow, operator by operator
kind: reference
status: draft
updated: 2026-11-04
---

# ISA levels above the baseline: a per-operator map

A note from the maintainer's discussion, for the orchestrator. **Nothing here is approved for
implementation**: an ISA variant needs its own spec (D24, D33; [S0005](specs/S0005-simd.md) Q4 for
SIMD). The maintainer has decided that we **will** add variants above the baseline later (D42).
This note fixes how we record and grow them, so the work started in S0005 (classes D/S/L at SSE2)
becomes the first column of a lasting map instead of a one-off count.

## Where the idea comes from

The Wasm SIMD specification names no ISA. Its text only says the operators are "a portable subset"
that maps "to commonly available SIMD instructions in hardware"
([SIMD.md](https://github.com/WebAssembly/simd/blob/main/proposals/simd/SIMD.md)). In practice the
community group admitted each operator with a written **mapping for every common ISA level**: a pull
request for a new operator carried a section "Mapping to Common Instruction Sets" with the exact
instruction sequence per level. For example, `i64x2.min_u`/`max_u`
([WebAssembly/simd#418](https://github.com/WebAssembly/simd/pull/418)) lists:

* x86/x86-64 with AVX512F+AVX512VL, with XOP, with AVX, with SSE4.2, with SSE4.1, with SSE2;
* ARM64;
* ARMv7 with NEON.

and under each heading the sequence (AVX: `vmovdqa` of a sign-bias constant, two `vpxor`,
`vpcmpgtq`, `vpblendvb`). The cost of an operator was judged from that table. That is why some
operators are cheap on SSE4.1 and NEON but long on SSE2 (our class L: `swizzle`/`shuffle` need SSSE3
`pshufb`; vector rounding needs SSE4.1 `roundps`).

Engines resolve it differently: V8 requires SSE4.1 for wasm SIMD, Cranelift runs on SSE2 with
libcalls for `pshufb` and rounding, and we run on SSE2 with native sequences for every operator
(S0005, D25: no computation helpers).

## The proposal: keep the same table for our own code

For every operator that has an ISA-sensitive lowering, we keep the **sequence we emit at each
level** we support or plan, in the same shape as those pull requests. One row per operator, one
column per level:

| operator | x86-64-v1 (SSE2) | x86-64-v2 (SSSE3, SSE4.1/4.2, POPCNT) | x86-64-v3 (AVX, AVX2, BMI1/2, LZCNT, FMA) | ARMv8.0-A | ARMv8.x (named features) |
|---|---|---|---|---|---|
| `i8x16.swizzle` | L: per-lane `cmp`/`sbb` + masked byte loads (~6 x 16) | `pshufb` + saturate-add bias (2-3) | `vpshufb` (2) | `tbl` (1) | - |
| `f32x4.ceil` | L: `cvttps2dq`/`cvtdq2ps` + fix + range/sign/NaN (~12) | `roundps` (1) | `vroundps` (1) | `frintp` (1) | - |
| `i32.popcnt` (scalar) | SWAR (~12) | `popcnt` (1) | `popcnt` (1) | `cnt` + `addv` via `v` regs (3-4) | - |
| `i32.clz` (scalar) | `bsr` + zero case (4) | same | `lzcnt` (1) | `clz` (1) | - |
| `f64.nearest` (scalar) | +/-2^52 add + sign fix (~8) | `roundsd` (1) | `vroundsd` (1) | `frintn` (1) | - |

(Sequences above are illustrative, written from S0005 and the current backends. The real table is
filled from the code, see "Rules".)

Each cell has: the instruction sequence, its length, the cost class at that level (D: 1-3
instructions, S: 4-12, L: longer or no direct form), and a status: **implemented**, **planned** or
**not worth it** (with the measurement that says so).

### What it is for

1. **Choosing which variant to add.** A level is worth adding where many hot operators drop from
   class S/L to D. The table shows the candidates at once; the SIMD benchmark of S0005 (and
   CoreMark for scalar ops) decides.
2. **One reviewable place per operator.** When a variant is added, the reviewer checks the new cell
   against the vendor manual and `llvm-mc`, as the CG checked each PR.
3. **The same table serves every architecture.** riscv64 (base RV64GC and RVV 1.0 as an optional
   level) and the micro-compilers ([MICRO-COMPILERS.md](MICRO-COMPILERS.md)) start from the same
   rows: an architecture adds columns, not a new document.
4. **Scalar operators too.** The baseline already pays for missing extensions in scalar code
   (`jit/x86_64/backend.py`: `bsr`/`bsf` with an explicit zero case, `popcnt` by SWAR, `round*`
   emulated for `ceil`/`floor`/`trunc`/`nearest`). Those rows belong in the same map.

## Rules (so the door stays open; no variant code before its spec)

* **Generate the table from the code; do not hand-write it.** The x86 `simd.py` is already
  table-driven (`FORMS`, `SHIFTS`, `PROG`). Give each program its ISA level explicitly (today every
  one is `v1`), so a variant is a second entry for the same operator, not a branch inside the first.
  A small tool, in the style of `tools/features.py`, renders `docs/ISA-MAP.md` from those tables;
  `--check` fails CI if it is stale. Until a second level exists, the tool is not needed: keep the
  explicit level field in the tables only.
  * Status (2026-11-02): x86 `CODE`/`SHIFTC`/`LOADC`/the shuffle table are keyed (operator, level),
    all v1; the inline paths listed in `jit/x86_64/simd.py`'s docstring are not. The first v2 spec
    adds: selection that falls back v2 -> v1 per operator, `SUPPORTED` per level, and level keys for
    the inline paths it replaces (the likely v2 candidates: `pshufb`, `ptest`, `pextrb`/`pinsrb`,
    `roundps`).
* **A variant enters only with a measurement** (D24): the SIMD benchmark or CoreMark shows a gain on
  the operators it changes. "Not worth it" is a valid recorded outcome.
* **Opt-in and declared** (D33): a level is chosen explicitly (`Config.isa`), the artifact declares
  it, and a CPU without the level refuses the artifact at load, naming the missing features. The
  baseline artifact stays **byte-identical** when a variant is added.
* **Each level has its own gate and run.** `tools/isa_all.py --level v2` already exists for x86;
  add `v3` when needed, and per-architecture levels for aarch64. At run time, a QEMU CPU model per
  level, as `qemu64` without SSSE3/SSE4.1 does for v1 today (e.g. `Nehalem` for v2, `Haswell` for
  v3). A blob with an instruction above its declared level fails CI.
* **Parity is unchanged.** Every level passes the same spec commands as `pyc` (D25). NaN bits may
  differ between levels as they may between architectures (compared by class, S0005).
* **Columns not planned**: XOP (AMD only, discontinued), ARMv7 NEON (32-bit, outside the JIT design;
  [TARGETS.md](TARGETS.md)). AVX-512 (x86-64-v4) only if a measurement asks for it: with AVX512VL
  it gives one-instruction forms for the 64-bit-lane operators that are long on every lower level
  (`i64x2.mul` `vpmullq`, `i64x2.abs` `vpabsq`, `i64x2` min/max `vpminuq`/`vpminsq`; that is why
  WebAssembly/simd#418 lists it first). Wider vectors (256-bit AVX2, SVE) do not help fixed 128-bit
  SIMD and are out of scope.

## Where it fits

* After **5d3** (class L on SSE2) and the SIMD benchmark: the first real column pair is x86-64-v1
  vs v2 for the 10 class-L operators (S0005 Q4 (b)).
* The aarch64 side has almost nothing to gain at 128 bits (ARMv8.0 NEON already has `tbl`,
  `frint*`, `cnt`); its later levels matter for other features (LSE atomics for threads, ARMv8.2
  dot product for relaxed SIMD).
* Relaxed SIMD (follow-up spec, S0005 Q7) is the place where levels matter most: its operators are
  defined to let each ISA use its fastest form (FMA, `pshufb` semantics, dot products), so its spec
  should start with this table.
* **Decided for the first relaxed-SIMD step (maintainer, 2026-11-04): the deterministic profile of
  Wasm 3.0 for 16 of the 20 operators** (every one takes the first of its allowed results: swizzle
  out of range 0, trunc saturating, min/max and q15mulr as the plain operators, dot the plain signed
  dot product), so each maps onto an operator we already have, the same on every target and in
  `pyc`. **`relaxed_madd`/`relaxed_nmadd` are native per target, as Wasmtime**: fused on `jit-aarch64`
  (`fmla`/`fmls`), unfused on `jit-x86_64` (the baseline has no FMA) and on `pyc`. x86-64 FMA comes
  with x86-64-v3 (FMA3, with AVX2), not with v2. Done at the baseline on all three targets (S0008
  steps 1-2): each JIT's table maps the 20 operators. **When ISA levels above the baseline are adopted,
  revisit relaxed SIMD**: a "native" mode (FMA on x86-64-v3, `pshufb` swizzle, hardware dot
  products) is where levels would pay, and it changes results across hosts (pyc stops being a
  bit-exact oracle for those operators, D35's cross-host identity needs the level in the artifact).
