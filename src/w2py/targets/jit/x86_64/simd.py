# pyright: reportPrivateUsage=false
"""SIMD (v128) on x86-64-v1, SSE2 only (docs/specs/S0005): the backend's `simd` method. A v128
lives in two value-stack slots; xmm0-xmm5 are scratch inside one call (xmm6-xmm15 are never
touched: callee-saved on Win64), so nothing vector-sized is live between calls. Encodings go
through the encoder's ModRM core; `encoder.py` does not change.

Data movement (step 5c): 16-byte moves by `movups` (slots are only 8-byte aligned, so no packed
operation takes a memory operand), bitwise operations on xmm0-xmm2, lanes in the slots by GPR
loads and stores (rcx, rdx: scratch as in every method), splat by a multiply with 0x0101...
`v128.const` is the core's (two constant halves).

Class D (step 5d1): every operator of 1-3 SSE2 instructions as a short program over xmm0-xmm2
(`PROG`): the operands are loaded into xmm0, xmm1 (wasm order), the destination of the last
step is the result. Shifts, `bitmask`, `all_true` and the extending loads add a GPR or a memory
step around a program. Sequences: Cranelift's x64 lowering for the SSE2 forms (S0005)."""

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
        psrlq d3 psraw e1 psrad e2 pshufd 70 cvtpd2ps 5a""",
    ),
    **_forms(b"", "movaps 28 andps 54 xorps 57 cvtdq2ps 5b cvtps2pd 5a"),
    **_forms(b"\xf3", "cvtdq2pd e6"),
}
for _n, _c in (("add", 0x58), ("sub", 0x5C), ("mul", 0x59), ("div", 0x5E), ("min", 0x5D),
               ("max", 0x5F), ("sqrt", 0x51), ("cmp", 0xC2)):  # fmt: skip
    FORMS[_n + "ps"], FORMS[_n + "pd"] = (b"", _c), (b"\x66", _c)
# shifts by an immediate: 66 0F 71/72/73 /ext ib
SHIFTS = {f"ps{d}{s}i": (0x71 + j, ext) for j, s in enumerate("wdq")
          for d, ext in (("rl", 2), ("ra", 4), ("ll", 6)) if (d, s) != ("ra", "q")}  # fmt: skip

# Programs over xmm0-xmm2 ("mnemonic d s [imm]; ..."): xmm0 = the first operand, xmm1 = the
# second; the last step's destination is the result.
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
# extending loads: 8 bytes by movq into xmm0, then the extend program
LOADS = {"load8x8_s": PROG["i16x8.extend_low_i8x16_s"],
         "load8x8_u": PROG["i16x8.extend_low_i8x16_u"],
         "load16x4_s": PROG["i32x4.extend_low_i16x8_s"],
         "load16x4_u": PROG["i32x4.extend_low_i16x8_u"],
         "load32x2_s": "movaps 1 0; psradi 1 31; punpckldq 0 1",  # interleave with the sign
         "load32x2_u": PROG["i64x2.extend_low_i32x4_u"]}  # fmt: skip
OTHER = [f"{sh}.{o}" for sh in ("i16x8", "i32x4", "i64x2") for o in ("shl", "shr_u", "shr_s")]
OTHER.remove("i64x2.shr_s")  # class S
OTHER.extend(f"{sh}.{o}" for sh in ("i8x16", "i16x8", "i32x4") for o in ("all_true", "bitmask"))
OTHER.extend(["i64x2.bitmask", *(f"v128.{k}" for k in LOADS)])
Step = tuple[str, int, int, int | None]


def _parse(prog: str) -> tuple[Step, ...]:
    out: list[Step] = []
    for step in prog.split(";"):
        name, *ns = step.split()
        assert name in FORMS or name in SHIFTS, name
        out.append((name, int(ns[0], 0), int(ns[1], 0), int(ns[2], 0) if len(ns) > 2 else None))
    return tuple(out)


CODE = {fs.BASE + sub: _parse(PROG[o.name]) for sub, o in fs.OPS.items() if o.name in PROG}
LOADC = {k: _parse(p) for k, p in LOADS.items()}
PACK = _parse("pxor 1 1; packsswb 0 1")  # i16x8.bitmask: signs into the low 8 bytes
ZEROS = {n: _parse(f"pxor 1 1; pcmpeq{s} 1 0") for n, s in ((1, "b"), (2, "w"), (4, "d"))}
SUPPORTED = MOVES | set(CODE) | {fs.BASE + s for s, o in fs.OPS.items() if o.name in OTHER}


def run(a: Asm, prog: tuple[Step, ...]) -> int:
    """Emit a program; the register that holds its result."""
    d = 0
    for name, d, s, imm in prog:
        if name in SHIFTS:  # psrlw/psraw/psllw ... xmm d, imm8 s
            code, ext = SHIFTS[name]
            a._op(bytes((0x0F, code)), ext, d, pre=b"\x66")
            a.buf.append(s)
            continue
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
    if op in CODE:
        for i, x in enumerate(args):
            movups(a, i, _slot(x), False)
        movups(a, run(a, CODE[op]), _slot(dst), True)
        return
    if kind in LOADC:
        movq_load(a, 0, b._bounds(f, args[0], imm[0], 8, imm[1]))
        movups(a, run(a, LOADC[kind]), _slot(dst), True)
        return
    if kind in ("shl", "shr_u", "shr_s"):  # the count first: it may be in tos (LESSONS 32)
        b.move(Reg(RCX), args[1], False)
        a.alu_imm("and", RCX, 8 * size - 1)
        a.movq_to_xmm(1, RCX, False)
        movups(a, 0, _slot(args[0]), False)
        name = {"shl": "psll", "shr_u": "psrl", "shr_s": "psra"}[kind]
        run(a, ((name + {2: "w", 4: "d", 8: "q"}[size], 0, 1, None),))
        movups(a, 0, _slot(dst), True)
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
