# pyright: basic, reportArgumentType=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
"""Fixed-width SIMD (v128) in the front end (docs/specs/S0005): the one operator table, the decoder
of the `0xFD` prefix and the validator of its operators. Shared by every target.

An instruction is the tuple `(0xFD000 + sub, immediates...)` (0xFD00 + sub would collide with a
later 0xFE prefix, S0005 Q6). Immediates: memory ops `(offset, align, mem)`, lane ops `(lane,)`,
lane memory ops `(offset, align, mem, lane)`, `v128.const` `(value,)` (an int in [0, 2^128), lane 0
in the low bits), `i8x16.shuffle` `(lanes,)` (16 bytes). Relaxed SIMD (sub 0x100..0x113) is a gap.
"""

from typing import NamedTuple

from w2py.frontend import binary as _b

BASE = 0xFD000
V, I32, I64, F32, F64 = 0x7B, 0x7F, 0x7E, 0x7D, 0x7C
NONE, MEM, LANE, MEMLANE, CONST, SHUFFLE = range(6)  # immediate kinds


class Op(NamedTuple):
    name: str
    imm: int
    pops: tuple[int, ...]  # operand types in wasm order (the last is on top)
    res: int  # result type, 0 for none
    n: int = 0  # lane count, for the `lane < n` check (LANE, MEMLANE)
    align: int = 0  # natural alignment as a power of two (MEM, MEMLANE)


OPS: dict[int, Op] = {}  # sub-opcode -> operator


def _put(sub, name, imm=NONE, pops=(), res=0, n=0, align=0):
    OPS[sub] = Op(name, imm, pops, res, n, align)


def _run(first, names, pops, res, prefix=""):
    """Consecutive sub-opcodes from `first`; a `-` is a reserved one."""
    for i, nm in enumerate(names.split()):
        if nm != "-":
            _put(first + i, f"{prefix}.{nm}" if prefix else nm, pops=pops, res=res)


def _build() -> None:
    U, B, S, T = (V,), (V, V), (V, I32), (V,)  # unary, binary, shift, test
    for sub, nm, al in (
        (0x00, "load", 4), (0x01, "load8x8_s", 3), (0x02, "load8x8_u", 3), (0x03, "load16x4_s", 3),
        (0x04, "load16x4_u", 3), (0x05, "load32x2_s", 3), (0x06, "load32x2_u", 3),
        (0x07, "load8_splat", 0), (0x08, "load16_splat", 1), (0x09, "load32_splat", 2),
        (0x0A, "load64_splat", 3), (0x5C, "load32_zero", 2), (0x5D, "load64_zero", 3),
    ):  # fmt: skip
        _put(sub, "v128." + nm, MEM, (I32,), V, align=al)
    _put(0x0B, "v128.store", MEM, (I32, V), 0, align=4)
    _put(0x0C, "v128.const", CONST, (), V)
    _put(0x0D, "i8x16.shuffle", SHUFFLE, B, V)
    _put(0x0E, "i8x16.swizzle", NONE, B, V)
    for sub, shape, lane_t in (
        (0x0F, "i8x16", I32), (0x10, "i16x8", I32), (0x11, "i32x4", I32),
        (0x12, "i64x2", I64), (0x13, "f32x4", F32), (0x14, "f64x2", F64),
    ):  # fmt: skip
        _put(sub, shape + ".splat", NONE, (lane_t,), V)
    for sub, shape, n, lane_t, sx in (
        (0x15, "i8x16", 16, I32, 1), (0x18, "i16x8", 8, I32, 1), (0x1B, "i32x4", 4, I32, 0),
        (0x1D, "i64x2", 2, I64, 0), (0x1F, "f32x4", 4, F32, 0), (0x21, "f64x2", 2, F64, 0),
    ):  # fmt: skip
        for i, nm in enumerate(("extract_lane_s", "extract_lane_u") if sx else ("extract_lane",)):
            _put(sub + i, f"{shape}.{nm}", LANE, U, lane_t, n)
        _put(sub + 1 + sx, shape + ".replace_lane", LANE, (V, lane_t), V, n)
    for sub, shape in ((0x23, "i8x16"), (0x2D, "i16x8"), (0x37, "i32x4")):
        _run(sub, "eq ne lt_s lt_u gt_s gt_u le_s le_u ge_s ge_u", B, V, shape)
    _run(0x41, "eq ne lt gt le ge", B, V, "f32x4")
    _run(0x47, "eq ne lt gt le ge", B, V, "f64x2")
    _put(0x4D, "v128.not", NONE, U, V)
    _run(0x4E, "and andnot or xor", B, V, "v128")
    _put(0x52, "v128.bitselect", NONE, (V, V, V), V)
    _put(0x53, "v128.any_true", NONE, T, I32)
    for sub, bits in ((0x54, 8), (0x55, 16), (0x56, 32), (0x57, 64)):
        n, al = 128 // bits, (bits // 8).bit_length() - 1
        _put(sub, f"v128.load{bits}_lane", MEMLANE, (I32, V), V, n, al)
        _put(sub + 4, f"v128.store{bits}_lane", MEMLANE, (I32, V), 0, n, al)
    _put(0x5E, "f32x4.demote_f64x2_zero", NONE, U, V)
    _put(0x5F, "f64x2.promote_low_f32x4", NONE, U, V)
    # i8x16
    _run(0x60, "abs neg popcnt", U, V, "i8x16")
    _run(0x63, "all_true bitmask", T, I32, "i8x16")
    _run(0x65, "narrow_i16x8_s narrow_i16x8_u", B, V, "i8x16")
    _run(0x6B, "shl shr_s shr_u", S, V, "i8x16")
    _run(0x6E, "add add_sat_s add_sat_u sub sub_sat_s sub_sat_u", B, V, "i8x16")
    _run(0x76, "min_s min_u max_s max_u", B, V, "i8x16")
    _put(0x7B, "i8x16.avgr_u", NONE, B, V)
    # rounding of f32x4 / f64x2 (scattered in the numbering)
    _run(0x67, "ceil floor trunc nearest", U, V, "f32x4")
    _run(0x74, "ceil floor", U, V, "f64x2")
    _run(0x7A, "trunc", U, V, "f64x2")
    _run(0x94, "nearest", U, V, "f64x2")
    _run(0x7C, "i16x8.extadd_pairwise_i8x16_s i16x8.extadd_pairwise_i8x16_u "
         "i32x4.extadd_pairwise_i16x8_s i32x4.extadd_pairwise_i16x8_u", U, V)  # fmt: skip
    # i16x8
    _run(0x80, "abs neg", U, V, "i16x8")
    _run(0x82, "q15mulr_sat_s", B, V, "i16x8")
    _run(0x83, "all_true bitmask", T, I32, "i16x8")
    _run(0x85, "narrow_i32x4_s narrow_i32x4_u", B, V, "i16x8")
    _run(0x87, "extend_low_i8x16_s extend_high_i8x16_s extend_low_i8x16_u extend_high_i8x16_u",
         U, V, "i16x8")  # fmt: skip
    _run(0x8B, "shl shr_s shr_u", S, V, "i16x8")
    _run(0x8E, "add add_sat_s add_sat_u sub sub_sat_s sub_sat_u", B, V, "i16x8")
    _run(0x95, "mul min_s min_u max_s max_u", B, V, "i16x8")
    _run(0x9B, "avgr_u extmul_low_i8x16_s extmul_high_i8x16_s extmul_low_i8x16_u "
         "extmul_high_i8x16_u", B, V, "i16x8")  # fmt: skip
    # i32x4
    _run(0xA0, "abs neg", U, V, "i32x4")
    _run(0xA3, "all_true bitmask", T, I32, "i32x4")
    _run(0xA7, "extend_low_i16x8_s extend_high_i16x8_s extend_low_i16x8_u extend_high_i16x8_u",
         U, V, "i32x4")  # fmt: skip
    _run(0xAB, "shl shr_s shr_u", S, V, "i32x4")
    _put(0xAE, "i32x4.add", NONE, B, V)
    _put(0xB1, "i32x4.sub", NONE, B, V)
    _run(0xB5, "mul min_s min_u max_s max_u dot_i16x8_s", B, V, "i32x4")
    _run(0xBC, "extmul_low_i16x8_s extmul_high_i16x8_s extmul_low_i16x8_u extmul_high_i16x8_u",
         B, V, "i32x4")  # fmt: skip
    # i64x2
    _run(0xC0, "abs neg", U, V, "i64x2")
    _run(0xC3, "all_true bitmask", T, I32, "i64x2")
    _run(0xC7, "extend_low_i32x4_s extend_high_i32x4_s extend_low_i32x4_u extend_high_i32x4_u",
         U, V, "i64x2")  # fmt: skip
    _run(0xCB, "shl shr_s shr_u", S, V, "i64x2")
    _put(0xCE, "i64x2.add", NONE, B, V)
    _put(0xD1, "i64x2.sub", NONE, B, V)
    _run(0xD5, "mul eq ne lt_s gt_s le_s ge_s extmul_low_i32x4_s extmul_high_i32x4_s "
         "extmul_low_i32x4_u extmul_high_i32x4_u", B, V, "i64x2")  # fmt: skip
    # f32x4, f64x2
    _run(0xE0, "abs neg", U, V, "f32x4")
    _put(0xE3, "f32x4.sqrt", NONE, U, V)
    _run(0xE4, "add sub mul div min max pmin pmax", B, V, "f32x4")
    _run(0xEC, "abs neg", U, V, "f64x2")
    _put(0xEF, "f64x2.sqrt", NONE, U, V)
    _run(0xF0, "add sub mul div min max pmin pmax", B, V, "f64x2")
    _run(0xF8, "i32x4.trunc_sat_f32x4_s i32x4.trunc_sat_f32x4_u f32x4.convert_i32x4_s "
         "f32x4.convert_i32x4_u i32x4.trunc_sat_f64x2_s_zero i32x4.trunc_sat_f64x2_u_zero "
         "f64x2.convert_low_i32x4_s f64x2.convert_low_i32x4_u", U, V)  # fmt: skip


_build()


def decode(r):
    """The instruction after the `0xFD` byte; `r` is the reader of `frontend.binary`."""
    sub = r.u()
    e = OPS.get(sub)
    if e is None:
        if 0x100 <= sub <= 0x113:  # relaxed SIMD: valid, not supported (a gap, not malformed)
            raise NotImplementedError(f"relaxed simd opcode {sub:#x}")
        raise _b.Malformed(f"illegal opcode 0xfd {sub}")
    ins = (BASE + sub,)
    if e.imm in (MEM, MEMLANE):
        al = r.u()
        if al >= 0x80:
            raise _b.Malformed("malformed memop flags")
        mi = 0
        if al & 0x40:
            mi = r.u()
            al &= 0x3F
        ins += (r.u64(), al, mi)
    if e.imm in (LANE, MEMLANE):
        ins += (r.u8(),)
    elif e.imm == CONST:
        ins += (int.from_bytes(r.bytes(16), "little"),)
    elif e.imm == SHUFFLE:
        ins += (bytes(r.bytes(16)),)
    return ins


def const(r):
    """A `0xFD` instruction in a constant expression: `v128.const` is one, any other is a gap."""
    sub = r.u()
    if sub != 0x0C:
        raise NotImplementedError(f"opcode 0xfd {sub} in a constant expression")
    return ("v128", int.from_bytes(r.bytes(16), "little"))


def validate(ins, pop, push, need_mem):
    """Type one operator: `pop(expected)` and `push(type)` act on the validator's operand stack,
    `need_mem(index)` checks that the memory exists."""
    from w2py.frontend.validate import Invalid  # validate imports this module

    e = OPS[ins[0] - BASE]
    if e.imm in (MEM, MEMLANE):
        need_mem(ins[3])
        if ins[2] > e.align:
            raise Invalid("alignment must not be larger than natural")
        if ins[1] > 0xFFFFFFFF:
            raise Invalid("offset out of range")
    if e.imm in (LANE, MEMLANE) and ins[-1] >= e.n:
        raise Invalid("invalid lane index")
    if e.imm == SHUFFLE and max(ins[1]) >= 32:
        raise Invalid("invalid lane index")
    for t in reversed(e.pops):
        pop(t)
    if e.res:
        push(e.res)


def uses(mod):
    """True if the validated module has a v128 type or a SIMD operator: a target without SIMD
    refuses it as `unsupported: simd`."""
    ts = [t for ps, rs in mod["types"] for t in (*ps, *rs)]
    ts += [g[0] for g in mod["globals"]] + [im[3][0] for im in mod["imports"] if im[0] == "global"]
    ts += [t for locs, _ in mod["codes"] for t in locs]
    return V in ts or any(ins[0] >= BASE for insns in mod["insns"] for ins in insns)
