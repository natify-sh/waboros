# pyright: reportPrivateUsage=false
"""SIMD (v128) on x86-64-v1, SSE2 only (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; xmm0-xmm5 are scratch inside one call (xmm6-xmm15 are never
touched: callee-saved on Win64), so nothing vector-sized is live between calls. Encodings go
through the encoder's ModRM core; `encoder.py` does not change.

Data movement (step 5c): 16-byte moves by `movups` (slots are only 8-byte aligned, so no packed
operation takes a memory operand), bitwise operations on xmm0-xmm2, lanes in the slots by GPR
loads and stores (rcx, rdx: scratch as in every method), splat by a multiply with 0x0101...
`v128.const` is the core's (two constant halves).

Classes D and S (steps 5d1, 5d2) and the class-L rounding operators (5d3): a short program over
xmm0-xmm5 (`PROG`, 1-25 instructions): the operands are loaded into xmm0, xmm1 (wasm order), the
destination of the last step is the result. A program's `k` step puts a 64-bit
constant into both halves of a register through rcx (no constant pool). Shifts (`SHIFT`: the
count masked in rcx, then `movd` into xmm1), `bitmask`, `all_true` and the extending loads add a
GPR or a memory step around a program. Sequences: Cranelift's x64 lowering for the SSE2 forms
(S0005).

Class L (5d3), no SSSE3/SSE4.1: rounding by the 2^23 / 2^52 magic add (a program, above);
`i8x16.shuffle` by one instruction when its lanes match one (`_shuffles`), else a byte gather
into the scratch slots (base + 6, 7); `i8x16.swizzle` per lane by `cmp`/`sbb` and a masked byte
load, into the scratch slots. Both copy the scratch slots to `dst` last.

ISA levels: `CODE`, `SHIFTC` and `LOADC` are keyed (operator, level) and `_SHUF` (lanes, level),
filled once; every entry is "v1" and selection is exact on (op, "v1"), with no fallback. Not
level-keyed today: `PACK`, `ZEROS`, the swizzle and shuffle gather paths, bitmask/all_true,
extract/replace/splat/bitselect/any_true, `MOVES`, `OTHER` (docs/ISA-LEVELS.md)."""

from typing import TYPE_CHECKING

from w2py.frontend import simd as fs
from w2py.targets.jit.common.backend import Dst, Operand, Reg, Slot
from w2py.targets.jit.common.simd import MOVES, shape
from w2py.targets.jit.x86_64.encoder import R12, R15, RCX, RDX, Asm, Mem

if TYPE_CHECKING:
    from w2py.targets.jit.x86_64.backend import Backend, Frame

SPLAT = {1: 0x0101010101010101, 2: 0x0001000100010001, 4: 0x0000000100000001}


# ---------------------------------------------------------------- SSE2 packed forms (0F xx /r)
def _forms(pre: bytes, text: str) -> dict[str, tuple[bytes, int]]:
    it = iter(text.split())
    return {name: (pre, int(code, 16)) for name, code in zip(it, it, strict=True)}


FORMS = {
    **_forms(
        b"\x66",
        """paddb fc paddw fd paddd fe paddq d4 psubb f8 psubw f9 psubd fa psubq fb
        paddsb ec paddsw ed paddusb dc paddusw dd psubsb e8 psubsw e9 psubusb d8 psubusw d9
        pminub da pmaxub de pminsw ea pmaxsw ee pavgb e0 pavgw e3 pmullw d5 pmaddwd f5 pmuludq f4
        pcmpeqb 74 pcmpeqw 75 pcmpeqd 76 pcmpgtb 64 pcmpgtw 65 pcmpgtd 66 pxor ef
        packsswb 63 packuswb 67 packssdw 6b punpcklbw 60 punpcklwd 61 punpckldq 62
        punpckhbw 68 punpckhwd 69 punpckhdq 6a psllw f1 pslld f2 psllq f3 psrlw d1 psrld d2
        psrlq d3 psraw e1 psrad e2 pshufd 70 cvtpd2ps 5a punpcklqdq 6c pand db pandn df por eb
        pmulhw e5 pmulhuw e4 cvttpd2dq e6""",
    ),
    **_forms(
        b"", "movaps 28 andps 54 andnps 55 orps 56 xorps 57 cvtdq2ps 5b cvtps2pd 5a shufps c6"
    ),
    **_forms(b"\xf3", "cvtdq2pd e6 cvttps2dq 5b movq 7e pshufhw 70"),
    **_forms(b"\xf2", "pshuflw 70"),
}
for _n, _c in (("add", 0x58), ("sub", 0x5C), ("mul", 0x59), ("div", 0x5E), ("min", 0x5D),
               ("max", 0x5F), ("sqrt", 0x51), ("cmp", 0xC2)):  # fmt: skip
    FORMS[_n + "ps"], FORMS[_n + "pd"] = (b"", _c), (b"\x66", _c)
# shifts by an immediate: 66 0F 71/72/73 /ext ib
SHIFTS = {f"ps{d}{s}i": (0x71 + j, ext) for j, s in enumerate("wdq")
          for d, ext in (("rl", 2), ("ra", 4), ("ll", 6)) if (d, s) != ("ra", "q")}  # fmt: skip
SHIFTS.update(psrldqi=(0x73, 3), pslldqi=(0x73, 7))  # whole register, by bytes

# Programs over xmm0-xmm5 ("mnemonic d s [imm]; ..."): xmm0 = the first operand, xmm1 = the
# second; the last step's destination is the result. `k d v`: the 64-bit v in both halves of
# xmm d, through rcx (programs with a scalar operand use none).
NOT = "pcmpeqb 2 2; pxor {} 2"  # all-ones, then xor
PROG: dict[str, str] = {}
for _sh, _s in (("i8x16", "b"), ("i16x8", "w"), ("i32x4", "d"), ("i64x2", "q")):
    PROG.update({f"{_sh}.add": f"padd{_s} 0 1", f"{_sh}.sub": f"psub{_s} 0 1",
                 f"{_sh}.neg": f"pxor 1 1; psub{_s} 1 0"})  # fmt: skip
    if _s in "bw":
        PROG.update({f"{_sh}.add_sat_s": f"padds{_s} 0 1", f"{_sh}.add_sat_u": f"paddus{_s} 0 1",
                     f"{_sh}.sub_sat_s": f"psubs{_s} 0 1", f"{_sh}.sub_sat_u": f"psubus{_s} 0 1",
                     f"{_sh}.avgr_u": f"pavg{_s} 0 1"})  # fmt: skip
    if _s in "bwd":  # lt = gt swapped; le = not gt; ge = not lt
        eq, gt = f"pcmpeq{_s}", f"pcmpgt{_s}"
        PROG.update({f"{_sh}.eq": f"{eq} 0 1", f"{_sh}.ne": f"{eq} 0 1; " + NOT.format(0),
                     f"{_sh}.gt_s": f"{gt} 0 1", f"{_sh}.lt_s": f"{gt} 1 0",
                     f"{_sh}.le_s": f"{gt} 0 1; " + NOT.format(0),
                     f"{_sh}.ge_s": f"{gt} 1 0; " + NOT.format(1)})  # fmt: skip
PROG.update({
    "i8x16.min_u": "pminub 0 1", "i8x16.max_u": "pmaxub 0 1",
    "i8x16.le_u": "pminub 1 0; pcmpeqb 0 1", "i8x16.ge_u": "pmaxub 1 0; pcmpeqb 0 1",
    "i8x16.abs": "pxor 1 1; psubb 1 0; pminub 0 1",  # min_u(x, -x); -128 stays 0x80
    "i8x16.narrow_i16x8_s": "packsswb 0 1", "i8x16.narrow_i16x8_u": "packuswb 0 1",
    "i16x8.mul": "pmullw 0 1", "i16x8.min_s": "pminsw 0 1", "i16x8.max_s": "pmaxsw 0 1",
    "i16x8.abs": "pxor 1 1; psubw 1 0; pmaxsw 0 1",  # max_s(x, -x)
    "i16x8.narrow_i32x4_s": "packssdw 0 1",
    "i32x4.abs": "movaps 1 0; psradi 1 31; pxor 0 1; psubd 0 1",  # (x ^ s) - s, s = x >> 31
    "i32x4.dot_i16x8_s": "pmaddwd 0 1",
    "i32x4.extadd_pairwise_i16x8_s": "pcmpeqw 1 1; psrlwi 1 15; pmaddwd 0 1",  # times 1 each
    # lanes 0, 1 (2, 3) of both into dwords 0 and 2, which pmuludq multiplies into 64 bits
    "i64x2.extmul_low_i32x4_u": "pshufd 0 0 0x50; pshufd 1 1 0x50; pmuludq 0 1",
    "i64x2.extmul_high_i32x4_u": "pshufd 0 0 0xfa; pshufd 1 1 0xfa; pmuludq 0 1",
    "f32x4.convert_i32x4_s": "cvtdq2ps 0 0", "f64x2.convert_low_i32x4_s": "cvtdq2pd 0 0",
    "f32x4.demote_f64x2_zero": "cvtpd2ps 0 0", "f64x2.promote_low_f32x4": "cvtps2pd 0 0",
})  # fmt: skip
for _sh, _src, _s, _k in (("i16x8", "i8x16", "bw", 8), ("i32x4", "i16x8", "wd", 16),
                          ("i64x2", "i32x4", "dq", 0)):  # fmt: skip
    for _h in ("low", "high"):
        up = f"punpck{_h[0]}{_s}"
        PROG[f"{_sh}.extend_{_h}_{_src}_u"] = f"pxor 1 1; {up} 0 1"
        if _k:  # each lane doubled, then shifted down arithmetically
            PROG[f"{_sh}.extend_{_h}_{_src}_s"] = f"{up} 0 0; psra{_s[1]}i 0 {_k}"
CMPS = (("eq", "0 1", 0), ("ne", "0 1", 4), ("lt", "0 1", 1), ("le", "0 1", 2),
        ("gt", "1 0", 1), ("ge", "1 0", 2))  # cmpps: eq lt le neq; gt, ge swapped  # fmt: skip
for _sh, _p, _w in (("f32x4", "ps", "d"), ("f64x2", "pd", "q")):
    PROG.update({f"{_sh}.{o}": f"{o}{_p} 0 1" for o in ("add", "sub", "mul", "div")})
    PROG.update({f"{_sh}.{o}": f"cmp{_p} {x} {imm}" for o, x, imm in CMPS})
    PROG.update(
        {
            f"{_sh}.sqrt": f"sqrt{_p} 0 0",
            f"{_sh}.pmin": f"min{_p} 1 0",  # b < a ? b : a: min returns its second operand unless <
            f"{_sh}.pmax": f"max{_p} 1 0",  # a < b ? b : a
            f"{_sh}.abs": f"pcmpeqb 1 1; psrl{_w}i 1 1; andps 0 1",
            f"{_sh}.neg": f"pcmpeqb 1 1; psll{_w}i 1 {31 if _w == 'd' else 63}; xorps 0 1",
        }
    )
# class S (step 5d2): 4-22 instructions; unsigned compares by a sign bias or a saturating
# subtract, i64x2 compares from 32-bit halves, blends by and/andn/or, products by pmuludq
SIGN32 = "pcmpeqd 3 3; pslldi 3 31; "  # 0x80000000 in each dword
GT64 = (  # a > b (x, y = 0, 1) or b > a (1, 0): high dwords signed; low ones biased, unsigned
    "pcmpeqd 2 2; psllqi 2 63; psrlqi 2 32; pxor 0 2; pxor 1 2; movaps 2 {}; pcmpgtd 2 {};"
    " pcmpeqd 0 1; pshufd 1 2 0xa0; pshufd 0 0 0xf5; pand 0 1; pshufd 1 2 0xf5; por 0 1"
)
SEL = "pand 1 {m}; pandn {m} 0; por {m} 1"  # mask m: b where set, else a
BIAS8 = "pcmpeqw 2 2; psllwi 2 15; packsswb 2 2; pxor 0 2; pxor 1 2; "  # 0x80 in each byte
BIAS16 = "pcmpeqw 2 2; psllwi 2 15; pxor 0 2; pxor 1 2; "
UNORD = "; movaps 3 0; cmp{p} 3 1 3; orps 2 3; psrl{w}i 3 {n}; andnps 3 2"  # NaN lanes canonical
PROG.update({
    "i8x16.gt_u": PROG["i8x16.le_u"] + "; " + NOT.format(0),
    "i8x16.lt_u": PROG["i8x16.ge_u"] + "; " + NOT.format(0),
    "i16x8.le_u": "psubusw 0 1; pxor 2 2; pcmpeqw 0 2",  # a <= b: a -sat b == 0
    "i16x8.ge_u": "psubusw 1 0; pxor 2 2; pcmpeqw 1 2",
    "i32x4.gt_u": SIGN32 + "pxor 0 3; pxor 1 3; pcmpgtd 0 1",
    "i32x4.lt_u": SIGN32 + "pxor 0 3; pxor 1 3; pcmpgtd 1 0",
    "i64x2.eq": "pcmpeqd 0 1; pshufd 1 0 0xb1; pand 0 1",
    "i64x2.gt_s": GT64.format(0, 1), "i64x2.lt_s": GT64.format(1, 0),
    "i64x2.abs": "pshufd 1 0 0xf5; psradi 1 31; pxor 0 1; psubq 0 1",
    "i8x16.min_s": BIAS8 + "pminub 0 1; pxor 0 2", "i8x16.max_s": BIAS8 + "pmaxub 0 1; pxor 0 2",
    "i16x8.min_u": BIAS16 + "pminsw 0 1; pxor 0 2", "i16x8.max_u": BIAS16 + "pmaxsw 0 1; pxor 0 2",
    "i32x4.min_s": "movaps 2 0; pcmpgtd 2 1; " + SEL.format(m=2),
    "i32x4.max_s": "movaps 2 1; pcmpgtd 2 0; " + SEL.format(m=2),
    "i32x4.min_u": SIGN32 + "movaps 4 0; pxor 4 3; pxor 3 1; pcmpgtd 4 3; " + SEL.format(m=4),
    "i32x4.max_u": SIGN32 + "movaps 4 1; pxor 4 3; pxor 3 0; pcmpgtd 4 3; " + SEL.format(m=4),
    # lanes 1, 3 multiplied apart; the low dwords of the four products interleaved
    "i32x4.mul": "pshufd 2 0 0xf5; pshufd 3 1 0xf5; pmuludq 0 1; pmuludq 2 3;"
                 " pshufd 0 0 0x08; pshufd 2 2 0x08; punpckldq 0 2",
    # lo*lo + (hi*lo + lo*hi) << 32
    "i64x2.mul": "movaps 2 0; psrlqi 2 32; pmuludq 2 1; movaps 3 1; psrlqi 3 32; pmuludq 3 0;"
                 " paddq 2 3; psllqi 2 32; pmuludq 0 1; paddq 0 2",
    "i16x8.extadd_pairwise_i8x16_s": "movaps 1 0; psllwi 1 8; psrawi 1 8; psrawi 0 8; paddw 0 1",
    "i16x8.extadd_pairwise_i8x16_u": "movaps 1 0; psllwi 1 8; psrlwi 1 8; psrlwi 0 8; paddw 0 1",
    "i32x4.extadd_pairwise_i16x8_u": "movaps 1 0; pslldi 1 16; psrldi 1 16; psrldi 0 16;"
                                     " paddd 0 1",
    # negative lanes to 0, minus 0x8000, signed saturation, plus 0x8000
    "i16x8.narrow_i32x4_u": "pxor 2 2; movaps 3 0; pcmpgtd 3 2; pand 0 3; movaps 3 1;"
                            " pcmpgtd 3 2; pand 1 3; pcmpeqd 4 4; psrldi 4 31; pslldi 4 15;"
                            " psubd 0 4; psubd 1 4; packssdw 0 1; pcmpeqw 4 4; psllwi 4 15;"
                            " pxor 0 4",
    # 32-bit products + 0x4000, >> 15, packed with signed saturation (only -1 * -1 saturates)
    "i16x8.q15mulr_sat_s": "movaps 2 0; pmullw 2 1; pmulhw 0 1; movaps 1 2; punpcklwd 2 0;"
                           " punpckhwd 1 0; pcmpeqd 3 3; psrldi 3 31; pslldi 3 14; paddd 2 3;"
                           " paddd 1 3; psradi 2 15; psradi 1 15; packssdw 2 1",
    # SWAR: bit pairs, nibbles, bytes
    "i8x16.popcnt": "k 3 0x5555555555555555; movaps 1 0; psrlwi 1 1; pand 1 3; psubb 0 1;"
                    " k 3 0x3333333333333333; movaps 1 0; psrlwi 1 2; pand 0 3; pand 1 3;"
                    " paddb 0 1; movaps 1 0; psrlwi 1 4; paddb 0 1; k 3 0x0f0f0f0f0f0f0f0f;"
                    " pand 0 3",
    # u32: (x - lo) / 2 converted exactly, doubled, plus lo: one rounding
    "f32x4.convert_i32x4_u": "movaps 1 0; pslldi 1 16; psrldi 1 16; psubd 0 1; psrldi 0 1;"
                             " cvtdq2ps 0 0; addps 0 0; cvtdq2ps 1 1; addps 0 1",
    # the u32 as the low mantissa bits of 2^52, minus 2^52
    "f64x2.convert_low_i32x4_u": "k 1 0x4330000043300000; punpckldq 0 1;"
                                 " k 1 0x4330000000000000; subpd 0 1",
    # NaN to 0; a positive input that came out INT_MIN flips to INT_MAX
    "i32x4.trunc_sat_f32x4_s": "movaps 1 0; cmpps 1 1 0; andps 0 1; pxor 1 0; cvttps2dq 0 0;"
                               " pand 1 0; psradi 1 31; pxor 0 1",
    # max(x, 0) (NaN to 0); x >= 2^31: 2^31 + cvt(x - 2^31); x >= 2^32: all ones
    "i32x4.trunc_sat_f32x4_u": "pxor 2 2; maxps 0 2; k 3 0x4f0000004f000000; movaps 1 0;"
                               " subps 1 3; movaps 4 3; cmpps 4 1 2; cmpps 3 0 2;"
                               " cvttps2dq 1 1; pand 1 3; pxor 1 4; cvttps2dq 0 0; paddd 0 1",
    "i32x4.trunc_sat_f64x2_s_zero": "movaps 1 0; cmppd 1 1 0; andps 0 1;"
                                    " k 2 0x41dfffffffc00000; minpd 0 2; cvttpd2dq 0 0",
    # clamp to [0, 2^32 - 1]; x + 2^52 rounds to nearest: minus 1 where that went up
    "i32x4.trunc_sat_f64x2_u_zero": "pxor 1 1; maxpd 0 1; k 1 0x41efffffffe00000; minpd 0 1;"
                                    " k 1 0x4330000000000000; movaps 2 0; addpd 2 1;"
                                    " movaps 3 2; subpd 3 1; cmppd 0 3 1; paddd 2 0;"
                                    " pshufd 2 2 0x08; movq 2 2",
})  # fmt: skip
for _sh, _x, _y in (("i16x8", "le_u", "gt_u"), ("i16x8", "ge_u", "lt_u"),
                    ("i32x4", "gt_u", "le_u"), ("i32x4", "lt_u", "ge_u"), ("i64x2", "eq", "ne"),
                    ("i64x2", "gt_s", "le_s"), ("i64x2", "lt_s", "ge_s")):  # fmt: skip
    _r = PROG[f"{_sh}.{_x}"].split(";")[-1].split()[1]  # the result register
    PROG[f"{_sh}.{_y}"] = PROG[f"{_sh}.{_x}"] + "; " + NOT.format(_r)
for _h, _c in (("low", "l"), ("high", "h")):
    PROG[f"i64x2.extend_{_h}_i32x4_s"] = f"movaps 1 0; psradi 1 31; punpck{_c}dq 0 1"
    PROG[f"i16x8.extmul_{_h}_i8x16_s"] = (
        f"punpck{_c}bw 0 0; psrawi 0 8; punpck{_c}bw 1 1; psrawi 1 8; pmullw 0 1"
    )
    PROG[f"i16x8.extmul_{_h}_i8x16_u"] = f"pxor 2 2; punpck{_c}bw 0 2; punpck{_c}bw 1 2; pmullw 0 1"
    for _u in ("s", "u"):  # low and high words of the products interleaved
        PROG[f"i32x4.extmul_{_h}_i16x8_{_u}"] = (
            f"movaps 2 0; pmullw 2 1; pmulh{'u' * (_u == 'u')}w 0 1; punpck{_c}wd 2 0"
        )
    # unsigned products minus (a < 0 ? b : 0) << 32 and (b < 0 ? a : 0) << 32
    _q = "0x50" if _h == "low" else "0xfa"
    PROG[f"i64x2.extmul_{_h}_i32x4_s"] = (
        f"pshufd 0 0 {_q}; pshufd 1 1 {_q}; movaps 2 0; psradi 2 31; pand 2 1; movaps 3 1;"
        " psradi 3 31; pand 3 0; paddd 2 3; psllqi 2 32; pmuludq 0 1; psubq 0 2"
    )
for _sh, _p, _w, _n in (("f32x4", "ps", "d", 10), ("f64x2", "pd", "q", 13)):
    # min/max both ways: equal operands (+-0) or'ed (min) / and'ed (max); unordered: a NaN
    for _o, _b in (("min", "or"), ("max", "and")):
        PROG[f"{_sh}.{_o}"] = (
            f"movaps 2 0; {_o}{_p} 2 1; movaps 3 1; {_o}{_p} 3 0;"
            f" {_b}ps 2 3" + UNORD.format(p=_p, w=_w, n=_n)
        )
# class L rounding (5d3), no roundps: xmm1 = |x|, xmm2 = the sign, xmm4 = |x| < 2^N (false for
# NaN), xmm3 = 2^N; (v + s) - s rounds v to nearest even (MXCSR's default), with v = x and
# s = 2^N of x's sign (floor, ceil) or v = |x|, s = 2^N (trunc, nearest); floor, ceil and
# trunc then add or subtract 1.0 (a compare mask shifted into 1.0) where that went the wrong
# way. The sign is or-ed back (-0, ceil(-0.5) = -0); lanes out of range keep x + 0: a NaN
# quieted as pyc's x * 1.0 does.
for _sh, _p, _w, _k, _one in (("f32x4", "ps", "d", 0x4B0000004B000000, 25),
                              ("f64x2", "pd", "q", 0x4330000000000000, 54)):  # fmt: skip
    _head = (f"pcmpeqb 2 2; psrl{_w}i 2 1; movaps 1 0; andps 1 2; andnps 2 0; k 3 {_k};"
             f" movaps 4 1; cmp{_p} 4 3 1; ")  # fmt: skip
    _r0 = f"movaps 5 {{}}; add{_p} 5 3; sub{_p} 5 3; "
    _fix = f"cmp{_p} 1 {{}} 1; psll{_w}i 1 {_one}; psrl{_w}i 1 2; {{}}{_p} 5 1; "
    _tail = f"orps 5 2; pxor 3 3; add{_p} 0 3; pand 5 4; pandn 4 0; por 4 5"
    PROG.update({
        f"{_sh}.floor": _head + "orps 3 2; " + _r0.format(0) + "movaps 1 0; "
                        + _fix.format(5, "sub") + _tail,  # x < r: r - 1
        f"{_sh}.ceil": _head + "orps 3 2; " + _r0.format(0) + "movaps 1 5; "
                       + _fix.format(0, "add") + _tail,  # r < x: r + 1
        f"{_sh}.trunc": _head + _r0.format(1) + _fix.format(5, "sub") + _tail,  # |x| < r: r - 1
        f"{_sh}.nearest": _head + _r0.format(1) + _tail,
    })  # fmt: skip
# extending loads: 8 bytes by movq into xmm0, then the extend program
LOADS = {"load8x8_s": PROG["i16x8.extend_low_i8x16_s"],
         "load8x8_u": PROG["i16x8.extend_low_i8x16_u"],
         "load16x4_s": PROG["i32x4.extend_low_i16x8_s"],
         "load16x4_u": PROG["i32x4.extend_low_i16x8_u"],
         "load32x2_s": PROG["i64x2.extend_low_i32x4_s"],
         "load32x2_u": PROG["i64x2.extend_low_i32x4_u"]}  # fmt: skip
# shifts: xmm0 = the value, xmm1 = the count (masked; + 8 for i8x16.shr_s)
SHIFT = {f"{sh}.{o}": f"{i}{s} 0 1" for sh, s in (("i16x8", "w"), ("i32x4", "d"), ("i64x2", "q"))
         for o, i in (("shl", "psll"), ("shr_u", "psrl"), ("shr_s", "psra"))}  # fmt: skip
SHIFT.update({
    # (x >>u n ^ m) - m, m = 2^63 >>u n: the sign extended
    "i64x2.shr_s": "pcmpeqd 2 2; psllqi 2 63; psrlq 2 1; psrlq 0 1; pxor 0 2; psubq 0 2",
    # words shifted, then the bits crossing a byte cleared: (0xff << n) or (0xff >> n) per byte
    "i8x16.shl": "psllw 0 1; pcmpeqb 2 2; psllw 2 1; punpcklbw 2 2; pshuflw 2 2 0; pshufd 2 2 0;"
                 " pand 0 2",
    "i8x16.shr_u": "psrlw 0 1; pcmpeqb 2 2; psrlw 2 1; psrlwi 2 8; packuswb 2 2; pand 0 2",
    # each byte into the high byte of a word, shifted by n + 8, packed back
    "i8x16.shr_s": "movaps 2 0; punpcklbw 0 0; punpckhbw 2 2; psraw 0 1; psraw 2 1;"
                   " packsswb 0 2",
})  # fmt: skip
OTHER = [f"{s}.{o}" for s in ("i8x16", "i16x8", "i32x4", "i64x2") for o in ("all_true", "bitmask")]
OTHER.extend([*(f"v128.{k}" for k in LOADS), "i8x16.shuffle", "i8x16.swizzle"])
Step = tuple[str, int, int, int | None]


def _parse(prog: str) -> tuple[Step, ...]:
    out: list[Step] = []
    for step in prog.split(";"):
        name, *ns = step.split()
        assert name in FORMS or name in SHIFTS or name == "k", name
        out.append((name, int(ns[0], 0), int(ns[1], 0), int(ns[2], 0) if len(ns) > 2 else None))
    return tuple(out)


LEVEL = "v1"  # the ISA level selection asks for; a variant is a second (operator, level) entry
CODE = {(fs.BASE + sub, LEVEL): _parse(PROG[o.name]) for sub, o in fs.OPS.items() if o.name in PROG}
LOADC = {(k, LEVEL): _parse(p) for k, p in LOADS.items()}
PACK = _parse("pxor 1 1; packsswb 0 1")  # i16x8.bitmask: signs into the low 8 bytes
ZEROS = {n: _parse(f"pxor 1 1; pcmpeq{s} 1 0") for n, s in ((1, "b"), (2, "w"), (4, "d"))}
ZEROS[8] = _parse("pxor 1 1; pcmpeqd 1 0; pshufd 2 1 0xb1; pand 1 2")  # both halves zero
SHIFTC = {
    (fs.BASE + sub, LEVEL): _parse(SHIFT[o.name]) for sub, o in fs.OPS.items() if o.name in SHIFT
}
SUPPORTED = (
    MOVES
    | {op for op, _ in CODE}
    | {op for op, _ in SHIFTC}
    | {fs.BASE + s for s, o in fs.OPS.items() if o.name in OTHER}
)


_SHUF: dict[tuple[tuple[int, ...], str], str] = {}


def _shuffles() -> dict[tuple[int, ...], str]:
    """`i8x16.shuffle` lanes -> a program over xmm0 = a, xmm1 = b that one SSE2 instruction
    (or a byte shift of each and `por`) does; the lanes follow from each instruction's
    definition. Built on the first shuffle."""
    out: dict[tuple[int, ...], str] = {}

    def add(prog: str, size: int, elems: list[int]) -> None:  # elements of `size` bytes
        out.setdefault(tuple(size * e + t for e in elems for t in range(size)), prog)

    for r in (0, 1):
        add(f"movaps 0 {r}", 1, [16 * r + i for i in range(16)])
        for imm in range(256):
            q = [imm >> 2 * j & 3 for j in range(4)]
            add(f"pshufd 0 {r} {imm}", 4, [4 * r + e for e in q])
            add(f"pshuflw 0 {r} {imm}", 2, [8 * r + e for e in (*q, 4, 5, 6, 7)])
            add(f"pshufhw 0 {r} {imm}", 2, [8 * r + e for e in (0, 1, 2, 3, *(4 + e for e in q))])
    for d, s in ((0, 1), (1, 0), (0, 0), (1, 1)):
        for imm in range(256):  # two dwords of d, then two of s
            q = [imm >> 2 * j & 3 for j in range(4)]
            add(f"shufps {d} {s} {imm}", 4, [4 * (d, d, s, s)[j] + q[j] for j in range(4)])
        for size, t in ((1, "bw"), (2, "wd"), (4, "dq")):  # qdq: shufps does the same
            n = 16 // size
            for h, lo in (("l", 0), ("h", n // 2)):  # the low or high halves interleaved
                ks = [k for i in range(lo, lo + n // 2) for k in (n * d + i, n * s + i)]
                add(f"punpck{h}{t} {d} {s}", size, ks)
    for n in range(1, 16):  # bytes n.. of one operand, then the first ones of the other
        for d, s in ((0, 1), (1, 0)):
            add(f"psrldqi {d} {n}; pslldqi {s} {16 - n}; por {d} {s}", 1,
                [(16 * d + n + i) % 32 for i in range(16)])  # fmt: skip
    return out


def run(a: Asm, prog: tuple[Step, ...]) -> int:
    """Emit a program; the register that holds its result."""
    d = 0
    for name, d, s, imm in prog:
        if name in SHIFTS:  # psrlw/psraw/psllw ... xmm d, imm8 s
            code, ext = SHIFTS[name]
            a._op(bytes((0x0F, code)), ext, d, pre=b"\x66")
            a.buf.append(s)
            continue
        if name == "k":  # mov rcx, s; movq xmm d, rcx; punpcklqdq xmm d, xmm d
            a.mov_imm(RCX, s, w=True)
            a.movq_to_xmm(d, RCX, True)
            name, s = "punpcklqdq", d
        pre, code = FORMS[name]
        a._op(bytes((0x0F, code)), d, s, pre=pre)
        if imm is not None:
            a.buf.append(imm)
    return d


def movq_load(a: Asm, x: int, m: Mem) -> None:
    """movq xmm, m64 (F3 0F 7E): the upper half zeroed."""
    a._op(b"\x0f\x7e", x, m, pre=b"\xf3")


def movmsk(a: Asm, r: int, x: int, size: int) -> None:
    """pmovmskb (size 1) / movmskps (4) / movmskpd (8) r32, xmm: one bit per lane."""
    code, pre = {1: (0xD7, b"\x66"), 4: (0x50, b""), 8: (0x50, b"\x66")}[size]
    a._op(bytes((0x0F, code)), r, x, pre=pre)


def movups(a: Asm, x: int, m: Mem, store: bool) -> None:
    """movups xmm, m128 (0F 10) / movups m128, xmm (0F 11): unaligned, any 16 bytes."""
    a._op(b"\x0f\x11" if store else b"\x0f\x10", x, m)


def andnps(a: Asm, d: int, s: int) -> None:
    """andnps xmm d, xmm s (0F 55): d = ~d & s."""
    a._op(b"\x0f\x55", d, s)


def _slot(o: Operand | None, disp: int = 0) -> Mem:
    assert isinstance(o, Slot), o  # a v128 is always in its slots here (S0005)
    return Mem(R12, 8 * o.k + disp)


def _ld(a: Asm, r: int, m: Mem, size: int) -> None:
    """r = the `size` bytes at m, zero-extended."""
    if size == 8:
        a.mov(r, m, w=True)
    elif size == 4:
        a.mov(r, m)
    else:
        a.movzx(r, m, 8 * size)


def _st(a: Asm, m: Mem, r: int, size: int) -> None:
    if size == 1:
        a.store8(m, r)
    elif size == 2:
        a.store16(m, r)
    else:
        a.mov(m, r, w=size == 8)


def _copy(a: Asm, src: Mem, to: Mem) -> None:
    if src != to:
        movups(a, 0, src, False)
        movups(a, 0, to, True)


def emit(
    b: "Backend", f: "Frame", op: int, dst: Dst | None, args: tuple[Operand, ...],
    imm: tuple[int, ...],
) -> None:  # fmt: skip
    a = b.a
    if op in (0x23, 0x24):  # (word, indirect): an imported global is a pointer to the owner's
        g = Mem(R15, 8 * imm[0])
        if imm[1]:
            a.mov(RCX, g, w=True)
            g = Mem(RCX)
        movups(a, 0, g if op == 0x23 else _slot(args[0]), False)
        movups(a, 0, _slot(dst) if op == 0x23 else g, True)
        return
    kind, size, signed = shape(op)
    if (op, LEVEL) in CODE:
        for i, x in enumerate(args):
            movups(a, i, _slot(x), False)
        movups(a, run(a, CODE[op, LEVEL]), _slot(dst), True)
        return
    if kind in ("shuffle", "swizzle"):  # class L (5d3): one instruction, or byte by byte
        if not _SHUF:
            _SHUF.update({(k, LEVEL): p for k, p in _shuffles().items()})
        lanes = tuple(imm[0] >> 8 * i & 255 for i in range(16)) if imm else ()
        if (lanes, LEVEL) in _SHUF:
            for i, x in enumerate(args):
                movups(a, i, _slot(x), False)
            movups(a, run(a, _parse(_SHUF[lanes, LEVEL])), _slot(dst), True)
            return
        sc = _slot(dst, 48)  # the scratch slots: disjoint from the operands, read first
        for i in range(16):
            if lanes:  # a byte of a or b
                a.movzx(RCX, _slot(args[lanes[i] >> 4], lanes[i] & 15), 8)
                a.store8(Mem(R12, sc.disp + i), RCX)
                continue
            # swizzle: a's byte at the index, and-ed with (index < 16 ? -1 : 0); cmp -> sbb
            a.movzx(RCX, _slot(args[1], i), 8)
            a.alu_imm("cmp", RCX, 16)
            a.alu("sbb", RDX, RDX)
            a.alu_imm("and", RCX, 15)
            a.alu("and", RDX, Mem(R12, _slot(args[0]).disp, RCX))  # up to 3 bytes past a: b's
            a.store8(Mem(R12, sc.disp + i), RDX)
        _copy(a, sc, _slot(dst))
        return
    if (kind, LEVEL) in LOADC:
        movq_load(a, 0, b._bounds(f, args[0], imm[0], 8, imm[1]))
        movups(a, run(a, LOADC[kind, LEVEL]), _slot(dst), True)
        return
    if (op, LEVEL) in SHIFTC:  # the count first: it may be in tos (LESSONS 32)
        b.move(Reg(RCX), args[1], False)
        a.alu_imm("and", RCX, 8 * size - 1)
        if size == 1 and kind == "shr_s":
            a.alu_imm("add", RCX, 8)
        a.movq_to_xmm(1, RCX, False)
        movups(a, 0, _slot(args[0]), False)
        movups(a, run(a, SHIFTC[op, LEVEL]), _slot(dst), True)
        return
    if kind in ("bitmask", "all_true"):  # i32 results by 32-bit writes: zero-extended
        assert isinstance(dst, Reg)
        movups(a, 0, _slot(args[0]), False)
        if kind == "bitmask":
            if size == 2:
                run(a, PACK)
            movmsk(a, dst.id, 0, 1 if size == 2 else size)
        else:  # no lane equal to zero; the flags of `test` are consumed by `sete`
            run(a, ZEROS[size])
            movmsk(a, RCX, 1, 1)
            a.test(RCX, RCX)
            a.setcc("e", RCX)
            a.movzx(dst.id, RCX, 8)
        return
    m = Mem(RCX)  # the memory access, if any: bounds checked, the address read first
    if kind in ("load", "store", "load_splat", "load_zero", "load_lane", "store_lane"):
        m = b._bounds(f, args[0], imm[0], size, imm[1])
    if kind == "load":
        _copy(a, m, _slot(dst))
    elif kind == "store":
        _copy(a, _slot(args[1]), m)
    elif kind in ("splat", "load_splat", "load_zero"):
        if kind == "splat":  # the lane's bits zero-extended into rdx
            b.move(Reg(RDX), args[0], size == 8)
            if size < 4:
                a.movzx(RDX, RDX, 8 * size)
        else:
            _ld(a, RDX, m, size)
        if kind == "load_zero":
            a.mov_imm(_slot(dst, 8), 0, w=True)
        elif size < 8:
            a.mov_imm(RCX, SPLAT[size], w=True)
            a.imul(RDX, RCX, w=True)
        a.mov(_slot(dst), RDX, w=True)
        if kind != "load_zero":
            a.mov(_slot(dst, 8), RDX, w=True)
    elif kind == "extract":  # an i32 result in its 32-bit form: zero-extended (LESSONS 5)
        assert isinstance(dst, Reg)
        v = _slot(args[0], imm[0] * size)
        if size >= 4 or not signed:
            _ld(a, dst.id, v, size)
        else:
            a.movsx(dst.id, v, 8 * size)
    elif kind == "replace":  # the scalar first: it may be in tos (LESSONS 32)
        b.move(Reg(RDX), args[1], size == 8)
        _copy(a, _slot(args[0]), _slot(dst))
        _st(a, _slot(dst, imm[0] * size), RDX, size)
    elif kind == "load_lane":  # dst overlaps the vector operand (one slot up): copied whole
        _ld(a, RDX, m, size)
        _copy(a, _slot(args[1]), _slot(dst))
        _st(a, _slot(dst, imm[2] * size), RDX, size)
    elif kind == "store_lane":
        _ld(a, RDX, _slot(args[1], imm[2] * size), size)
        _st(a, m, RDX, size)
    elif kind == "not":
        for h in (0, 8):
            a.mov(RCX, _slot(args[0], h), w=True)
            a.unary("not", RCX, w=True)
            a.mov(_slot(dst, h), RCX, w=True)
    elif kind in ("and", "andnot", "or", "xor", "bitselect"):
        for i, x in enumerate(args):
            movups(a, i, _slot(x), False)
        r = 0
        if kind == "andnot":  # x & ~y
            andnps(a, 1, 0)
            r = 1
        elif kind == "bitselect":  # (x & c) | (y & ~c)
            a.sse("and", 0, 2, False)
            andnps(a, 2, 1)
            a.sse("or", 0, 2, False)
        else:
            a.sse(kind, 0, 1, False)  # andps / orps / xorps
        movups(a, r, _slot(dst), True)
    elif kind == "any_true":  # the flags of `or` are consumed by the next instruction
        assert isinstance(dst, Reg)
        a.mov(RCX, _slot(args[0]), w=True)
        a.alu("or", RCX, _slot(args[0], 8), w=True)
        a.setcc("ne", RCX)
        a.movzx(dst.id, RCX, 8)
    else:
        raise AssertionError(f"SIMD opcode {op:#x}")
