# pyright: reportPrivateUsage=false
"""SIMD (v128) on ARMv8.0-A Advanced SIMD (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; v0-v7 and v16-v31 are scratch inside one call (v8-v15 are never
touched: their low halves are callee-saved, AAPCS64), so nothing vector-sized is live between
calls. Encodings are emitted as words here; `encoder.py` does not change.

Data movement (step 5c): 16-byte moves by `ldr`/`str q`, bitwise operations on v0-v2, lanes in
the slots by GPR loads and stores (x9-x13, x17: scratch as in every method), splat by `dup`.
`v128.const` is the core's (two constant halves).

Integer operators (step 5e1): a short program over v0-v7 (`PROG`, 1-10 instructions; NEON forms in
`FORMS`, each a fixed word plus its llvm-mc spelling): the operands are loaded into v0, v1 (wasm
order), the destination of the last step is the result; a `k` step puts a 64-bit constant into
both halves of a register through x9. Shifts dup the masked count (negated for right shifts) and
shift by `sshl`/`ushl`; `bitmask`, `all_true`, the extending loads and `i64x2.mul` (no 64-bit lane
multiply: both lanes through x9-x12) add a GPR or a memory step around a program.

Float operators, conversions, rounding and `swizzle` (step 5e2) are programs too (`fmin`/`fmax`
have wasm's NaN and +-0 rules, `fcvtzs`/`fcvtzu` saturate, `frintp/m/z/n`, `tbl`); `shuffle`
puts its 16 lane indices into v2 through x9, x10 and runs a two-register `tbl` over v0, v1.
Sequences: S0005 "JIT, aarch64"."""

from typing import TYPE_CHECKING

from waboros.frontend import simd as fs
from waboros.targets.jit.aarch64.abi import CTX, FRAME
from waboros.targets.jit.aarch64.encoder import ZR, Asm
from waboros.targets.jit.common.backend import Dst, Operand, Reg, Slot
from waboros.targets.jit.common.simd import MOVES, shape

if TYPE_CHECKING:
    from waboros.targets.jit.aarch64.backend import Backend, Frame

ARR = {"i8x16": "16b", "i16x8": "8h", "i32x4": "4s", "i64x2": "2d"}
SZ = {"16b": 0, "8h": 1, "4s": 2, "2d": 3}
HALF = {"8h": "8b", "4s": "4h", "2d": "2s"}  # the 64-bit half of a widening form's source
FULL = {"8b": "16b", "4h": "8h", "2s": "4s"}  # its 128-bit form (the `2` variants)
# NEON forms: name -> (word without registers, llvm-mc text with {d} {n} {m})
FORMS: dict[str, tuple[int, str]] = {}


def _forms() -> None:
    def same(mn: str, u: int, opc: int, ts: str) -> None:  # three same, 128 bits
        for t in ts.split():
            word = 0x4E200400 | u << 29 | SZ[t] << 22 | opc << 11
            FORMS[f"{mn}.{t}"] = (word, f"{mn} v{{d}}.{t}, v{{n}}.{t}, v{{m}}.{t}")

    def misc(mn: str, u: int, opc: int, q: int, size: int, td: str, tn: str, k: str = "") -> None:
        word = 0x0E200800 | q << 30 | u << 29 | size << 22 | opc << 12
        FORMS[f"{mn}{k}.{td}"] = (word, f"{mn} v{{d}}.{td}, v{{n}}.{tn}{k and ', #0'}")

    ALL, BH, BHS = "16b 8h 4s 2d", "16b 8h", "16b 8h 4s"
    for mn, u, opc, ts in (("add", 0, 0x10, ALL), ("sub", 1, 0x10, ALL), ("sqadd", 0, 1, BH),
                           ("uqadd", 1, 1, BH), ("sqsub", 0, 5, BH), ("uqsub", 1, 5, BH),
                           ("urhadd", 1, 2, BH), ("smax", 0, 0xC, BHS), ("umax", 1, 0xC, BHS),
                           ("smin", 0, 0xD, BHS), ("umin", 1, 0xD, BHS), ("cmgt", 0, 6, ALL),
                           ("cmhi", 1, 6, ALL), ("cmge", 0, 7, ALL), ("cmhs", 1, 7, ALL),
                           ("cmeq", 1, 0x11, ALL), ("mul", 0, 0x13, "8h 4s"),
                           ("sqrdmulh", 1, 0x16, "8h"), ("sshl", 0, 8, ALL), ("ushl", 1, 8, ALL),
                           ("addp", 0, 0x17, "4s"), ("and", 0, 3, "16b")):  # fmt: skip
        same(mn, u, opc, ts)
    for t in ALL.split():
        misc("abs", 0, 0xB, 1, SZ[t], t, t)
        misc("neg", 1, 0xB, 1, SZ[t], t, t)
    misc("cnt", 0, 5, 1, 0, "16b", "16b")
    misc("mvn", 1, 5, 1, 0, "16b", "16b")
    misc("cmeq", 0, 9, 1, 3, "2d", "2d", "z")  # cmeqz.2d: compare with #0
    for t, n in (("8h", "16b"), ("4s", "8h")):
        misc("saddlp", 0, 2, 1, SZ[n], t, n)
        misc("uaddlp", 1, 2, 1, SZ[n], t, n)
    for t in ("8h", "4s", "2d"):  # narrowing: 64-bit half (q = 0) or the upper one (2, q = 1)
        h = HALF[t]
        if t != "2d":
            misc("xtn", 0, 0x12, 0, SZ[t] - 1, h, t)
            for mn, u, opc in (("sqxtn", 0, 0x14), ("sqxtun", 1, 0x12)):
                misc(mn, u, opc, 0, SZ[t] - 1, h, t)
                misc(mn + "2", u, opc, 1, SZ[t] - 1, FULL[h], t)
        for mn, u in (("sxtl", 0), ("uxtl", 1)):  # sshll/ushll #0, immh:immb = source bits
            for q in (0, 1):
                word = 0x0F00A400 | q << 30 | u << 29 | 8 << SZ[t] - 1 << 16
                src = FULL[h] if q else h
                FORMS[f"{mn}{'2' * q}.{t}"] = (word, f"{mn}{'2' * q} v{{d}}.{t}, v{{n}}.{src}")
        for mn, u in (("smull", 0), ("umull", 1)):
            for q in (0, 1):
                src = FULL[h] if q else h
                word = 0x0E20C000 | q << 30 | u << 29 | (SZ[t] - 1) << 22
                FORMS[f"{mn}{'2' * q}.{t}"] = (
                    word, f"{mn}{'2' * q} v{{d}}.{t}, v{{n}}.{src}, v{{m}}.{src}")  # fmt: skip
    for t in ("16b", "8h", "4s"):  # sshr #(lane bits - 1): each lane's sign in all its bits
        word = 0x4F000400 | (16 << SZ[t]) - (8 << SZ[t]) + 1 << 16
        FORMS[f"sshr.{t}"] = (word, f"sshr v{{d}}.{t}, v{{n}}.{t}, #{(8 << SZ[t]) - 1}")
    for mn, u, opc, q, size, t, r in (("addv", 0, 0x1B, 0, 0, "8b", "b"),
                                      ("addv", 0, 0x1B, 0, 1, "4h", "h"),
                                      ("addv", 0, 0x1B, 1, 1, "8h", "h"),
                                      ("uminv", 1, 0x1A, 1, 0, "16b", "b"),
                                      ("uminv", 1, 0x1A, 1, 1, "8h", "h"),
                                      ("uminv", 1, 0x1A, 1, 2, "4s", "s"),
                                      ("umaxv", 1, 0xA, 1, 2, "4s", "s")):  # fmt: skip
        word = 0x0E300800 | q << 30 | u << 29 | size << 22 | opc << 12
        FORMS[f"{mn}.{t}"] = (word, f"{mn} {r}{{d}}, v{{n}}.{t}")
    FORMS["ext8.16b"] = (0x6E004000, "ext v{d}.16b, v{n}.16b, v{m}.16b, #8")
    FORMS["zip1.16b"] = (0x4E003800, "zip1 v{d}.16b, v{n}.16b, v{m}.16b")
    FORMS["bsl.16b"] = (0x6E601C00, "bsl v{d}.16b, v{n}.16b, v{m}.16b")
    FORMS["tbl1.16b"] = (0x4E000000, "tbl v{d}.16b, {{v{n}.16b}}, v{m}.16b")
    FORMS["tbl2.16b"] = (0x4E002000, "tbl v{d}.16b, {{v{n}.16b, v{n1}.16b}}, v{m}.16b")
    # floats (step 5e2): bit 23 (`a`) and sz (bit 22: .2d) instead of the integer size field
    for t, sz in (("4s", 0), ("2d", 1)):
        for mn, u, a1, opc in (("fadd", 0, 0, 0x1A), ("fsub", 0, 1, 0x1A), ("fmul", 1, 0, 0x1B),
                               ("fdiv", 1, 0, 0x1F), ("fmax", 0, 0, 0x1E), ("fmin", 0, 1, 0x1E),
                               ("fcmeq", 0, 0, 0x1C), ("fcmge", 1, 0, 0x1C),
                               ("fcmgt", 1, 1, 0x1C)):  # fmt: skip
            word = 0x4E200400 | u << 29 | a1 << 23 | sz << 22 | opc << 11
            FORMS[f"{mn}.{t}"] = (word, f"{mn} v{{d}}.{t}, v{{n}}.{t}, v{{m}}.{t}")
        for mn, u, a1, opc in (("fabs", 0, 1, 0xF), ("fneg", 1, 1, 0xF), ("fsqrt", 1, 1, 0x1F),
                               ("frintn", 0, 0, 0x18), ("frintm", 0, 0, 0x19),
                               ("frintp", 0, 1, 0x18), ("frintz", 0, 1, 0x19),
                               ("fcvtzs", 0, 1, 0x1B), ("fcvtzu", 1, 1, 0x1B),
                               ("scvtf", 0, 0, 0x1D), ("ucvtf", 1, 0, 0x1D)):  # fmt: skip
            misc(mn, u, opc, 1, a1 << 1 | sz, t, t)
    misc("fcvtn", 0, 0x16, 0, 1, "2s", "2d")  # the upper half zeroed
    misc("fcvtl", 0, 0x17, 0, 1, "2d", "2s")
    misc("sqxtn", 0, 0x14, 0, 2, "2s", "2d")
    misc("uqxtn", 1, 0x14, 0, 2, "2s", "2d")


_forms()
# Programs over v0-v7 ("form d n [m]; ..."): v0 = the first operand, v1 = the second; the last
# step's destination is the result. `k d v`: the 64-bit v in both halves of v d, through x9.
PROG: dict[str, str] = {}
for _sh, _t in ARR.items():
    PROG.update({f"{_sh}.add": f"add.{_t} 0 0 1", f"{_sh}.sub": f"sub.{_t} 0 0 1",
                 f"{_sh}.neg": f"neg.{_t} 0 0", f"{_sh}.abs": f"abs.{_t} 0 0",
                 f"{_sh}.eq": f"cmeq.{_t} 0 0 1", f"{_sh}.ne": f"cmeq.{_t} 0 0 1; mvn.16b 0 0",
                 f"{_sh}.gt_s": f"cmgt.{_t} 0 0 1", f"{_sh}.lt_s": f"cmgt.{_t} 0 1 0",
                 f"{_sh}.ge_s": f"cmge.{_t} 0 0 1", f"{_sh}.le_s": f"cmge.{_t} 0 1 0"})  # fmt: skip
    if _t != "2d":
        PROG.update({f"{_sh}.gt_u": f"cmhi.{_t} 0 0 1", f"{_sh}.lt_u": f"cmhi.{_t} 0 1 0",
                     f"{_sh}.ge_u": f"cmhs.{_t} 0 0 1", f"{_sh}.le_u": f"cmhs.{_t} 0 1 0",
                     **{f"{_sh}.{o}_{s}": f"{s}{o}.{_t} 0 0 1"
                        for o in ("min", "max") for s in "su"}})  # fmt: skip
    if _t in ("16b", "8h"):
        PROG.update({f"{_sh}.{o}_sat_{s}": f"{s}q{o}.{_t} 0 0 1"
                     for o in ("add", "sub") for s in "su"})  # fmt: skip
        PROG[f"{_sh}.avgr_u"] = f"urhadd.{_t} 0 0 1"
    if _t != "16b":
        _src = {"8h": "i8x16", "4s": "i16x8", "2d": "i32x4"}[_t]
        for _h, _2 in (("low", ""), ("high", "2")):
            for _u in "su":
                PROG[f"{_sh}.extend_{_h}_{_src}_{_u}"] = f"{_u}xtl{_2}.{_t} 0 0"
                PROG[f"{_sh}.extmul_{_h}_{_src}_{_u}"] = f"{_u}mull{_2}.{_t} 0 0 1"
PROG.update({
    "i16x8.mul": "mul.8h 0 0 1", "i32x4.mul": "mul.4s 0 0 1", "i8x16.popcnt": "cnt.16b 0 0",
    "i16x8.q15mulr_sat_s": "sqrdmulh.8h 0 0 1",
    "i32x4.dot_i16x8_s": "smull.4s 2 0 1; smull2.4s 3 0 1; addp.4s 0 2 3",
    **{f"{t}.extadd_pairwise_{s}_{u}": f"{u}addlp.{a} 0 0"
       for t, s, a in (("i16x8", "i8x16", "8h"), ("i32x4", "i16x8", "4s")) for u in "su"},
    "i8x16.narrow_i16x8_s": "sqxtn.8b 2 0; sqxtn2.16b 2 1",
    "i8x16.narrow_i16x8_u": "sqxtun.8b 2 0; sqxtun2.16b 2 1",
    "i16x8.narrow_i32x4_s": "sqxtn.4h 2 0; sqxtn2.8h 2 1",
    "i16x8.narrow_i32x4_u": "sqxtun.4h 2 0; sqxtun2.8h 2 1",
})  # fmt: skip
# floats (5e2): fmin/fmax have wasm's NaN and +-0 rules; pmin/pmax keep an operand's bits
# (b < a ? b : a, a < b ? b : a: an fcmgt mask, then bsl); fcvtzs/fcvtzu saturate, NaN -> 0
for _sh, _t in (("f32x4", "4s"), ("f64x2", "2d")):
    for _o in ("add", "sub", "mul", "div", "min", "max", "abs", "neg", "sqrt"):
        PROG[f"{_sh}.{_o}"] = f"f{_o}.{_t} 0 0" + " 1" * (_o not in ("abs", "neg", "sqrt"))
    for _o, _r in (("ceil", "p"), ("floor", "m"), ("trunc", "z"), ("nearest", "n")):
        PROG[f"{_sh}.{_o}"] = f"frint{_r}.{_t} 0 0"
    PROG.update({f"{_sh}.eq": f"fcmeq.{_t} 0 0 1", f"{_sh}.ne": f"fcmeq.{_t} 0 0 1; mvn.16b 0 0",
                 f"{_sh}.gt": f"fcmgt.{_t} 0 0 1", f"{_sh}.lt": f"fcmgt.{_t} 0 1 0",
                 f"{_sh}.ge": f"fcmge.{_t} 0 0 1", f"{_sh}.le": f"fcmge.{_t} 0 1 0",
                 f"{_sh}.pmin": f"fcmgt.{_t} 2 0 1; bsl.16b 2 1 0",
                 f"{_sh}.pmax": f"fcmgt.{_t} 2 1 0; bsl.16b 2 1 0"})  # fmt: skip
PROG.update({
    "i32x4.trunc_sat_f32x4_s": "fcvtzs.4s 0 0", "i32x4.trunc_sat_f32x4_u": "fcvtzu.4s 0 0",
    "f32x4.convert_i32x4_s": "scvtf.4s 0 0", "f32x4.convert_i32x4_u": "ucvtf.4s 0 0",
    # to 64 bits saturated, then narrowed saturating into the low half (the upper one zeroed)
    "i32x4.trunc_sat_f64x2_s_zero": "fcvtzs.2d 0 0; sqxtn.2s 0 0",
    "i32x4.trunc_sat_f64x2_u_zero": "fcvtzu.2d 0 0; uqxtn.2s 0 0",
    "f64x2.convert_low_i32x4_s": "sxtl.2d 0 0; scvtf.2d 0 0",
    "f64x2.convert_low_i32x4_u": "uxtl.2d 0 0; ucvtf.2d 0 0",
    "f32x4.demote_f64x2_zero": "fcvtn.2s 0 0", "f64x2.promote_low_f32x4": "fcvtl.2d 0 0",
    "i8x16.swizzle": "tbl1.16b 0 0 1",  # an index >= 16 gives 0, as in wasm
})  # fmt: skip
SHUFFLE = (("tbl2.16b", 3, 0, 2),)  # v0, v1 = a, b (a consecutive table); v2 = the lanes
INS_D1 = 0x4E181C00  # ins (mov) vd.d[1], xn
W8 = 0x8040201008040201  # bit i of byte i
BITMASK = {  # each lane's sign spread, and-ed with its bit weight, summed across (bytes)
    1: f"sshr.16b 0 0; k 1 {W8}; and.16b 0 0 1; ext8.16b 1 0 0; zip1.16b 0 0 1; addv.8h 0 0",
    2: f"sshr.8h 0 0; xtn.8b 0 0; k 1 {W8}; and.16b 0 0 1; addv.8b 0 0",
    4: "sshr.4s 0 0; xtn.4h 0 0; k 1 0x0008000400020001; and.16b 0 0 1; addv.4h 0 0",
}
ALL_TRUE = {1: "uminv.16b 0 0", 2: "uminv.8h 0 0", 4: "uminv.4s 0 0",
            8: "cmeqz.2d 0 0; umaxv.4s 0 0"}  # the minimum lane; 2d: any lane zero  # fmt: skip
LOADS = {f"load{a}x{b}_{u}": PROG[f"{t}.extend_low_{s}_{u}"] for a, b, t, s in
         ((8, 8, "i16x8", "i8x16"), (16, 4, "i32x4", "i16x8"), (32, 2, "i64x2", "i32x4"))
         for u in "su"}  # fmt: skip
SHIFT = {f"{sh}.{o}": (i, o != "shl") for sh in ARR
         for o, i in (("shl", "ushl"), ("shr_s", "sshl"), ("shr_u", "ushl"))}  # fmt: skip
Step = tuple[str, int, int, int]


def _parse(prog: str) -> tuple[Step, ...]:
    out: list[Step] = []
    for step in prog.split(";"):
        name, *ns = step.split()
        assert name in FORMS or name == "k", name
        assert len(ns) < 3 or "{m}" in FORMS[name][1], step  # Rm of a 2-register form is fixed
        out.append((name, int(ns[0], 0), int(ns[1], 0), int(ns[2], 0) if len(ns) > 2 else 0))
    return tuple(out)


def run(a: Asm, prog: tuple[Step, ...]) -> int:
    """Emit a program; the vector register that holds its result."""
    d = 0
    for name, d, n, m in prog:
        if name == "k":
            a.mov_imm(9, n)
            dup(a, d, 9, 8)
        else:
            a.emit(FORMS[name][0] | m << 16 | n << 5 | d)
    return d


def ld_d(a: Asm, t: int, base: int, off: int, index: int | None) -> None:
    """ldr d`t`, [base, #off] (off a multiple of 8, < 32 KiB) or [base, index]: the upper half
    of v`t` zeroed."""
    if index is None:
        a.emit(0xFD400000 | off // 8 << 10 | base << 5 | t)
    else:
        a.emit(0xFC606800 | index << 16 | base << 5 | t)


CODE = {fs.BASE + sub: _parse(PROG[o.name]) for sub, o in fs.OPS.items() if o.name in PROG}
SHIFTC = {fs.BASE + sub: (_parse(f"{SHIFT[o.name][0]}.{ARR[o.name[:5]]} 0 0 1"), SHIFT[o.name][1])
          for sub, o in fs.OPS.items() if o.name in SHIFT}  # fmt: skip
BITMASKC = {size: _parse(p) for size, p in BITMASK.items()}
ALL_TRUEC = {size: _parse(p) for size, p in ALL_TRUE.items()}
LOADC = {k: _parse(p) for k, p in LOADS.items()}
OTHER = {"i64x2.mul", "i8x16.shuffle", *(f"v128.{k}" for k in LOADS)}
OTHER.update(f"{sh}.{o}" for sh in ARR for o in ("bitmask", "all_true"))
SUPPORTED = (
    MOVES | set(CODE) | set(SHIFTC) | {fs.BASE + s for s, o in fs.OPS.items() if o.name in OTHER}
)
# Advanced SIMD, 16 bytes (`.16b`): d = n op m; NOT d = ~n; BSL d = (d & n) | (~d & m)
VOP = {"and": 0x4E201C00, "andnot": 0x4E601C00, "or": 0x4EA01C00, "xor": 0x6E201C00,
       "bsl": 0x6E601C00, "not": 0x6E205800}  # fmt: skip


def ldst_q(a: Asm, load: bool, t: int, base: int, off: int = 0, index: int | None = None) -> None:
    """ldr/str q`t`, [base, #off] (scaled), ldur/stur for a small unaligned offset, or
    [base, index]; any other offset goes through x17."""
    if index is None and off % 16 == 0 and 0 <= off < 16 << 12:
        a.emit((0x3DC00000 if load else 0x3D800000) | off // 16 << 10 | base << 5 | t)
        return
    if index is None and -256 <= off < 256:
        a.emit((0x3CC00000 if load else 0x3C800000) | (off & 0x1FF) << 12 | base << 5 | t)
        return
    if index is None:
        a.mov_imm(17, off)
        index = 17
    else:
        assert off == 0
    a.emit((0x3CE06800 if load else 0x3CA06800) | index << 16 | base << 5 | t)


def vop(a: Asm, op: str, d: int, n: int, m: int = 0) -> None:
    """and/bic/orr/eor/bsl/not (mvn) vd.16b, vn.16b[, vm.16b] (`andnot` is bic)."""
    a.emit(VOP[op] | m << 16 | n << 5 | d)


def dup(a: Asm, d: int, r: int, size: int) -> None:
    """dup vd.<16b|8h|4s|2d>, w/x r (the low `size` bytes of r into every lane)."""
    a.emit(0x4E000C00 | size << 16 | r << 5 | d)


def _slot(o: Operand | None, disp: int = 0) -> int:
    assert isinstance(o, Slot), o  # a v128 is always in its slots here (S0005)
    return 8 * o.k + disp


def _lane(a: Asm, load: bool, t: int, off: int, size: int, signed: bool = False) -> None:
    """t = / := the `size` bytes at [frame + off] (a multiple of size; through x17 if far)."""
    wide = size == 8 or not signed
    if off // size < 1 << 12:
        (a.ld(t, FRAME, off, size, signed, wide) if load else a.st(t, FRAME, off, size))
    else:
        a.mov_imm(17, off)
        (a.ld_idx(t, FRAME, 17, size, signed, wide) if load else a.st_idx(t, FRAME, 17, size))


def _mem(a: Asm, load: bool, t: int, acc: tuple[int, int | None, int], size: int) -> None:
    """t = / := the `size` bytes of a bounds-checked access (base, index or None, offset)."""
    base, index, off = acc
    if index is None:
        (a.ld(t, base, off, size) if load else a.st(t, base, off, size))
    else:
        (a.ld_idx(t, base, index, size) if load else a.st_idx(t, base, index, size))


def _copy(a: Asm, src: int, to: int) -> None:
    if src != to:
        ldst_q(a, True, 0, FRAME, src)
        ldst_q(a, False, 0, FRAME, to)


def emit(
    b: "Backend", f: "Frame", op: int, dst: Dst | None, args: tuple[Operand, ...],
    imm: tuple[int, ...],
) -> None:  # fmt: skip
    a = b.a
    if op in (0x23, 0x24):  # (word, indirect): an imported global is a pointer to the owner's
        g, off = CTX, 8 * imm[0]
        if imm[1]:
            b._word(True, 9, CTX, off)
            g, off = 9, 0
        if op == 0x23:
            ldst_q(a, True, 0, g, off)
            ldst_q(a, False, 0, FRAME, _slot(dst))
        else:
            ldst_q(a, True, 0, FRAME, _slot(args[0]))
            ldst_q(a, False, 0, g, off)
        return
    kind, size, signed = shape(op)
    if op in CODE:
        for i, x in enumerate(args):
            ldst_q(a, True, i, FRAME, _slot(x))
        ldst_q(a, False, run(a, CODE[op]), FRAME, _slot(dst))
        return
    if op in SHIFTC:  # the count first: it may be in tos (LESSONS 32); negative: right
        prog, right = SHIFTC[op]
        a.logic_imm("and", 10, b._r(args[1], 10, False), 8 * size - 1, False)
        if right:
            a.addsub("sub", 10, ZR, 10, False)
        dup(a, 1, 10, size)
        ldst_q(a, True, 0, FRAME, _slot(args[0]))
        ldst_q(a, False, run(a, prog), FRAME, _slot(dst))
        return
    if kind == "shuffle":  # the 16 lane indices (< 32) into v2 through x9, x10
        for i, x in enumerate(args):
            ldst_q(a, True, i, FRAME, _slot(x))
        a.mov_imm(9, imm[0] & (1 << 64) - 1)
        a.mov_imm(10, imm[0] >> 64)
        a.fmov_to(2, 9)
        a.emit(INS_D1 | 10 << 5 | 2)
        ldst_q(a, False, run(a, SHUFFLE), FRAME, _slot(dst))
        return
    if kind in LOADC:  # 8 bytes into d0 (the upper half zeroed), then the extend
        base, index, off = b._bounds(f, args[0], imm[0], 8, imm[1])
        ld_d(a, 0, base, off, index)
        ldst_q(a, False, run(a, LOADC[kind]), FRAME, _slot(dst))
        return
    if kind == "mul":  # i64x2: no 64-bit lane multiply; both lanes through GPRs, read first
        for r, x, h in ((9, args[0], 0), (10, args[1], 0), (11, args[0], 8), (12, args[1], 8)):
            _lane(a, True, r, _slot(x, h), 8)
        a.madd(9, 9, 10, ZR)
        a.madd(11, 11, 12, ZR)
        _lane(a, False, 9, _slot(dst), 8)
        _lane(a, False, 11, _slot(dst, 8), 8)
        return
    if kind in ("bitmask", "all_true"):  # i32 results by 32-bit writes: zero-extended
        assert isinstance(dst, Reg)
        if kind == "bitmask" and size == 8:  # the two sign bits through GPRs
            _lane(a, True, 9, _slot(args[0]), 8)
            _lane(a, True, 10, _slot(args[0], 8), 8)
            a.bfm("ubfm", 9, 9, 63, 63)  # lsr #63
            a.bfm("ubfm", 10, 10, 63, 63)
            a.logic("orr", dst.id, 9, 10, False, "lsl", 1)
            return
        ldst_q(a, True, 0, FRAME, _slot(args[0]))
        v = run(a, (BITMASKC if kind == "bitmask" else ALL_TRUEC)[size])
        if kind == "bitmask":
            a.fmov_from(dst.id, v, False)
            return
        a.fmov_from(9, v, False)  # the flags of `cmp` are consumed by the next instruction
        a.cmp(9, ZR, False)
        a.cset(dst.id, "eq" if size == 8 else "ne")
        return
    acc: tuple[int, int | None, int] = (0, None, 0)  # the memory access, if any
    if kind in ("load", "store", "load_splat", "load_zero", "load_lane", "store_lane"):
        acc = b._bounds(f, args[0], imm[0], size, imm[1])  # the address read first (x9-x12)
    base, index, off = acc
    if kind == "load":
        ldst_q(a, True, 0, base, off, index)
        ldst_q(a, False, 0, FRAME, _slot(dst))
    elif kind == "store":
        ldst_q(a, True, 0, FRAME, _slot(args[1]))
        ldst_q(a, False, 0, base, off, index)
    elif kind in ("splat", "load_splat"):
        if kind == "splat":
            r = b._r(args[0], 10, size == 8)
        else:
            r = 10
            _mem(a, True, r, acc, size)
        dup(a, 0, r, size)
        ldst_q(a, False, 0, FRAME, _slot(dst))
    elif kind == "load_zero":
        _mem(a, True, 10, acc, size)
        _lane(a, False, ZR, _slot(dst, 8), 8)
        _lane(a, False, 10, _slot(dst), 8)
    elif kind == "extract":  # an i32 result as a w register: zero-extended (LESSONS 5)
        assert isinstance(dst, Reg)
        _lane(a, True, dst.id, _slot(args[0], imm[0] * size), size, signed)
    elif kind == "replace":  # the scalar first: it may be in tos (LESSONS 32)
        r = b._r(args[1], 10, size == 8)
        _copy(a, _slot(args[0]), _slot(dst))
        _lane(a, False, r, _slot(dst, imm[0] * size), size)
    elif kind == "load_lane":  # dst overlaps the vector operand (one slot up): copied whole
        _mem(a, True, 10, acc, size)
        _copy(a, _slot(args[1]), _slot(dst))
        _lane(a, False, 10, _slot(dst, imm[2] * size), size)
    elif kind == "store_lane":
        _lane(a, True, 13, _slot(args[1], imm[2] * size), size)
        _mem(a, False, 13, acc, size)
    elif kind in VOP or kind == "bitselect":
        for i, x in enumerate(args):
            ldst_q(a, True, i, FRAME, _slot(x))
        r = 0
        if kind == "bitselect":  # v2 = c: (c & x) | (~c & y)
            vop(a, "bsl", 2, 0, 1)
            r = 2
        else:
            vop(a, kind, 0, 0, 0 if kind == "not" else 1)
        ldst_q(a, False, r, FRAME, _slot(dst))
    elif kind == "any_true":  # the flags of `cmp` are consumed by the next instruction
        assert isinstance(dst, Reg)
        _lane(a, True, 9, _slot(args[0]), 8)
        _lane(a, True, 10, _slot(args[0], 8), 8)
        a.logic("orr", 9, 9, 10)
        a.cmp(9, ZR)
        a.cset(dst.id, "ne")
    else:
        raise AssertionError(f"SIMD opcode {op:#x}")
