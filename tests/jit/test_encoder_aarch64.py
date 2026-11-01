"""The aarch64 encoder, form by form, against `llvm-mc -triple=aarch64 -show-encoding` (S0004);
runs on any host that has llvm-mc (the x86-64 `jit` job and the arm64 jobs)."""

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.jit import need
from w2py.targets.jit.aarch64.encoder import (
    ADDSUB,
    COND,
    CSEL,
    DP2,
    EXTEND,
    LOGIC,
    SP,
    ZR,
    Asm,
    OutOfRange,
    bitmask,
    patch,
)

LLVM_MC = shutil.which("llvm-mc")


def _llvm_objcopy() -> str | None:
    """llvm-objcopy (GNU objcopy on x86-64 cannot read aarch64 ELF): on PATH, next to llvm-mc, or
    versioned (llvm-objcopy-18) on PATH."""
    mc = os.path.realpath(LLVM_MC) if LLVM_MC else None
    found = shutil.which("llvm-objcopy") or (
        mc and shutil.which("llvm-objcopy", path=Path(mc).parent)
    )
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not found and os.path.isdir(d):
            found = next((str(Path(d) / f) for f in sorted(os.listdir(d), reverse=True)
                          if re.fullmatch(r"llvm-objcopy-\d+(\.exe)?", f)), None)  # fmt: skip
    return found or None


OBJCOPY = _llvm_objcopy()
Form = tuple[Callable[[Asm], None], str]


def r(n: int, wide: bool = True) -> str:
    names = {SP: "sp", ZR: "xzr"} if wide else {SP: "wsp", ZR: "wzr"}
    return names.get(n, f"{'x' if wide else 'w'}{n}")


def bitmasks(width: int) -> list[int]:
    """Every logical immediate of the width: element 2-64 bits, run of 1..size-1 ones, rotated."""
    out: list[int] = []
    for size in (2, 4, 8, 16, 32, 64):
        if size > width:
            break
        for ones in range(1, size):
            for rot in range(size):
                e = ((1 << ones) - 1) << rot
                e = (e | e >> size) & ((1 << size) - 1)
                out.append(sum(e << i for i in range(0, width, size)))
    return out


def forms() -> list[Form]:
    out: list[Form] = []

    def add(text: str, emit: Callable[[Asm], None]) -> None:
        out.append((emit, text))

    for w in (False, True):
        bits = 64 if w else 32
        for op in ADDSUB:
            s = op.endswith("s")
            for d, n in ((0, 1), (30, SP), (ZR if s else SP, 29), (ZR if s else SP, SP)):
                for k in (0, 1, 4095, 4096, 0xFFF000, 0x7000):
                    add(
                        f"{op} {r(d, w)}, {r(n, w)}, #{k}",
                        lambda a, op=op, d=d, n=n, k=k, w=w: a.addsub_imm(op, d, n, k, w),
                    )
            for d, n, m in ((0, 1, 2), (30, ZR, 29), (ZR, 7, ZR)):
                for sh in ("lsl", "lsr", "asr"):
                    for amt in (0, 1, bits - 1):
                        add(
                            f"{op} {r(d, w)}, {r(n, w)}, {r(m, w)}, {sh} #{amt}",
                            lambda a, op=op, d=d, n=n, m=m, w=w, sh=sh, amt=amt: a.addsub(
                                op, d, n, m, w, sh, amt
                            ),
                        )
            for d, n in ((0, 1), (ZR if s else SP, SP)):
                for ext in EXTEND:
                    mw = w and ext.endswith("x")
                    for amt in (0, 2, 4):
                        add(
                            f"{op} {r(d, w)}, {r(n, w)}, {r(3, mw)}, {ext} #{amt}",
                            lambda a, op=op, d=d, n=n, w=w, ext=ext, amt=amt: a.addsub_ext(
                                op, d, n, 3, ext, amt, w
                            ),
                        )
        add(f"cmp {r(SP, w)}, {r(16, w)}", lambda a, w=w: a.cmp(SP, 16, w))
        add(f"cmp {r(30, w)}, {r(17, w)}", lambda a, w=w: a.cmp(30, 17, w))
        for op in LOGIC:
            for sh in ("lsl", "lsr", "asr", "ror"):
                for amt in (0, 5, bits - 1):
                    add(
                        f"{op} {r(4, w)}, {r(ZR, w)}, {r(30, w)}, {sh} #{amt}",
                        lambda a, op=op, w=w, sh=sh, amt=amt: a.logic(op, 4, ZR, 30, w, sh, amt),
                    )
            for i, v in enumerate(bitmasks(bits)):
                d = (SP if op != "ands" else ZR) if i % 7 == 0 else i % 29
                add(
                    f"{op} {r(d, w)}, {r(i % 31, w)}, #{v:#x}",
                    lambda a, op=op, d=d, i=i, v=v, w=w: a.logic_imm(op, d, i % 31, v, w),
                )
        for d, n in ((0, 1), (SP, 16), (16, SP), (29, SP), (ZR, 5), (5, ZR)):
            if not (SP in (d, n) and not w):
                add(f"mov {r(d, w)}, {r(n, w)}", lambda a, d=d, n=n, w=w: a.mov(d, n, w))
        for op in ("movz", "movn", "movk"):
            for k in (0, 1, 0x1234, 0xFFFF):
                for sh in range(0, bits, 16):
                    add(
                        f"{op} {r(9, w)}, #{k}, lsl #{sh}",
                        lambda a, op=op, k=k, sh=sh, w=w: a.movw(op, 9, k, sh, w),
                    )
        for sub in (False, True):
            add(
                f"{'msub' if sub else 'madd'} {r(1, w)}, {r(2, w)}, {r(30, w)}, {r(ZR, w)}",
                lambda a, w=w, sub=sub: a.madd(1, 2, 30, ZR, w, sub),
            )
        for op in DP2:
            add(
                f"{op} {r(0, w)}, {r(17, w)}, {r(30, w)}",
                lambda a, op=op, w=w: a.dp2(op, 0, 17, 30, w),
            )
        for op in ("rbit", "clz"):
            add(f"{op} {r(3, w)}, {r(28, w)}", lambda a, op=op, w=w: a.dp1(op, 3, 28, w))
        for op in ("sbfm", "ubfm"):
            for ir, iss in (
                (0, 0),
                (0, bits - 1),
                (bits - 1, bits - 1),
                (3, 7),
                (1, 31),
                (bits - 8, 15),
            ):
                add(
                    f"{op} {r(6, w)}, {r(7, w)}, #{ir}, #{iss}",
                    lambda a, op=op, ir=ir, iss=iss, w=w: a.bfm(op, 6, 7, ir, iss, w),
                )
        for lsb in (0, 1, bits - 1):
            add(
                f"extr {r(1, w)}, {r(2, w)}, {r(2, w)}, #{lsb}",
                lambda a, lsb=lsb, w=w: a.extr(1, 2, 2, lsb, w),
            )
        for op in CSEL:
            for c in COND:
                add(
                    f"{op} {r(0, w)}, {r(1, w)}, {r(ZR, w)}, {c}",
                    lambda a, op=op, c=c, w=w: a.csel(op, 0, 1, ZR, c, w),
                )
        for c in [*COND][:-1]:
            add(f"cset {r(11, w)}, {c}", lambda a, c=c, w=w: a.cset(11, c, w))
        for neg in (False, True):
            for c in ("eq", "hi", "le"):
                for nzcv in (0, 15):
                    mn = "ccmn" if neg else "ccmp"
                    add(
                        f"{mn} {r(1, w)}, {r(30, w)}, #{nzcv}, {c}",
                        lambda a, c=c, nzcv=nzcv, w=w, neg=neg: a.ccmp(1, 30, nzcv, c, w, neg=neg),
                    )
                    for k in (0, 1, 31):
                        add(
                            f"{mn} {r(1, w)}, #{k}, #{nzcv}, {c}",
                            lambda a, c=c, k=k, nzcv=nzcv, w=w, neg=neg: a.ccmp(
                                1, k, nzcv, c, w, imm=True, neg=neg
                            ),
                        )
    # loads and stores: (mnemonic, size, load, signed, wide)
    kinds = [("ldrb", 1, True, False, False), ("ldrh", 2, True, False, False),
             ("ldr", 4, True, False, False), ("ldr", 8, True, False, True),
             ("ldrsb", 1, True, True, False), ("ldrsb", 1, True, True, True),
             ("ldrsh", 2, True, True, False), ("ldrsh", 2, True, True, True),
             ("ldrsw", 4, True, True, True), ("strb", 1, False, False, False),
             ("strh", 2, False, False, False), ("str", 4, False, False, False),
             ("str", 8, False, False, True)]  # fmt: skip
    for mn, size, load, signed, w in kinds:
        t = r(0 if load else ZR, w)
        for base in (1, 28, SP):
            for off in (0, size, 4095 * size, -256, 255, 1, -8):
                add(
                    f"{mn} {t}, [{r(base)}, #{off}]",
                    lambda a, s=size, ld=load, sg=signed, w=w, b=base, o=off: (
                        a.ld(0, b, o, s, sg, w) if ld else a.st(ZR, b, o, s)
                    ),
                )
        for ext, xi in (("lsl", True), ("uxtw", False), ("sxtw", False), ("sxtx", True)):
            for scaled in (False, True):
                amt = f", {ext} #{size.bit_length() - 1}" if scaled else f", {ext}" * (ext != "lsl")
                add(
                    f"{mn} {t}, [{r(SP)}, {r(30, xi)}{amt}]",
                    lambda a, s=size, ld=load, sg=signed, w=w, e=ext, sc=scaled: (
                        a.ld_idx(0, SP, 30, s, sg, w, e, sc)
                        if ld
                        else a.st_idx(ZR, SP, 30, s, e, sc)
                    ),
                )
    for load in (False, True):
        for base in (SP, 28):
            for off in (-512, -16, 0, 8, 504):
                for mode, fmt in (
                    ("off", "[{b}, #{o}]"),
                    ("pre", "[{b}, #{o}]!"),
                    ("post", "[{b}], #{o}"),
                ):
                    add(
                        f"{'ldp' if load else 'stp'} x29, x30, " + fmt.format(b=r(base), o=off),
                        lambda a, ld=load, b=base, o=off, m=mode: a.pair(ld, 29, 30, b, o, m),
                    )
    for n in range(31):
        add(f"br x{n}", lambda a, n=n: a.br(n))
        add(f"blr x{n}", lambda a, n=n: a.blr(n))
        add(f"ret x{n}", lambda a, n=n: a.ret(n))
        add(f"fmov d{31 - n}, x{n}", lambda a, n=n: a.fmov_to(31 - n, n))
        add(f"fmov s{n}, w{n}", lambda a, n=n: a.fmov_to(n, n, False))
        add(f"fmov x{n}, d{n + 1}", lambda a, n=n: a.fmov_from(n, n + 1))
        add(f"fmov w{n}, s{n}", lambda a, n=n: a.fmov_from(n, n, False))
        add(f"cnt v{n}.8b, v{31 - n}.8b", lambda a, n=n: a.cnt8b(n, 31 - n))
        add(f"addv b{n + 1}, v{n}.8b", lambda a, n=n: a.addv8b(n + 1, n))
    add("bti c", lambda a: a.bti("c"))
    add("bti j", lambda a: a.bti("j"))
    return out


def llvm_mc(lines: list[str]) -> list[bytes]:
    assert LLVM_MC
    p = subprocess.run(
        [LLVM_MC, "-triple=aarch64", "-mattr=+bti", "-show-encoding"],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0 and not p.stderr, p.stderr[:2000]
    encs = re.findall(r"encoding: \[([^\]]*)\]", p.stdout)
    return [bytes(int(b, 16) for b in e.split(",")) for e in encs]


def encode(emit: Callable[[Asm], None]) -> bytes:
    a = Asm()
    emit(a)
    return a.finish()


def test_forms_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    fs = forms()
    ours = [encode(e) for e, _ in fs]
    theirs = llvm_mc([t for _, t in fs])
    assert len(theirs) == len(fs)
    bad = [
        f"{t}: ours {o.hex()} llvm-mc {m.hex()}"
        for (_, t), o, m in zip(fs, ours, theirs, strict=True)
        if o != m
    ]
    assert not bad, f"{len(bad)} of {len(fs)} forms differ:\n" + "\n".join(bad[:30])
    print(f"{len(fs)} aarch64 encoder forms checked against llvm-mc")


VALUES = [0, 1, 42, 0xFFFF, 0x10000, 0x12345678, 0xFFFFFFFF, 0x80000000, 0xFFFF0000FFFF,
          0x5555555555555555, 0x00FF00FF00FF00FF, 0x123456789ABCDEF0, 0xFFFFFFFFFFFF1234,
          0xFFFF1234FFFF5678, 0x8000000000000000, (1 << 64) - 1, (1 << 64) - 0x10001]  # fmt: skip


def test_mov_imm_is_exact_and_shortest() -> None:
    """Disassembled by llvm-mc (movz/movn/orr print as `mov #value`), the sequence gives the value,
    in at most as many instructions as non-trivial 16-bit chunks, all movz/movn/movk/orr (no
    flag-setting form)."""
    need(LLVM_MC, "llvm-mc")
    assert LLVM_MC
    for w in (False, True):
        for v in VALUES:
            v &= (1 << (64 if w else 32)) - 1
            code = encode(lambda a, v=v, w=w: a.mov_imm(5, v, w))
            hexes = " ".join(f"0x{b:02x}" for b in code)
            p = subprocess.run(
                [LLVM_MC, "-triple=aarch64", "--disassemble"],
                input=hexes,
                capture_output=True,
                text=True,
                check=True,
            )
            got = seen = 0
            for line in p.stdout.split("\n"):
                m = re.match(r"\s*(mov|movk)\s+[wx]5, #(-?\w+)(?:, lsl #(\d+))?", line)
                if m:
                    seen += 1
                    k = int(m[2], 0) & (1 << (64 if w else 32)) - 1
                    sh = int(m[3] or 0)
                    got = k if m[1] == "mov" else got & ~(0xFFFF << sh) | k << sh
            chunks = [v >> s & 0xFFFF for s in range(0, 64 if w else 32, 16)]
            most = len(chunks) - max(chunks.count(0), chunks.count(0xFFFF))
            assert got == v and seen == len(code) // 4 <= max(1, most), (hex(v), p.stdout)


def test_bitmask_rejects() -> None:
    for v, w in ((0, True), ((1 << 64) - 1, True), (0xFFFFFFFF, False), (5, True), (0x1234, False)):
        assert bitmask(v, w) is None
    with pytest.raises(ValueError):
        Asm().logic_imm("and", 0, 1, 0x1234)


def test_branch_range_ends_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    cases = [("b", 0x14000000, "b26", 1 << 27), ("b.eq", 0x54000000, "b19", 1 << 20),
             ("cbz w3,", 0x34000003, "b19", 1 << 20), ("tbz x5, #40,", 0xB6400005, "b14", 1 << 15),
             ("adr x1,", 0x10000001, "adr", 1 << 20)]  # fmt: skip
    texts: list[str] = []
    ours: list[bytes] = []
    for mn, word, kind, lim in cases:
        for rel in (-lim, -4, 0, 4, lim - 4) if kind != "adr" else (-lim, -1, 1, lim - 1):
            texts.append(f"{mn} #{rel}")
            ours.append(patch(word, kind, rel).to_bytes(4, "little"))
        for rel in (-lim - 4, lim):
            with pytest.raises(OutOfRange):
                patch(word, kind, rel)
    for pages in (-(1 << 20), 1, (1 << 20) - 1):
        texts.append(f"adrp x16, #{pages * 4096}")
        ours.append(patch(0x90000010, "adrp", pages).to_bytes(4, "little"))
    assert llvm_mc(texts) == ours


def test_labels_match_llvm_mc(tmp_path: Path) -> None:
    """Forward and backward branches of every kind through labels, against llvm-mc's object."""
    need(LLVM_MC and OBJCOPY, "llvm-mc/llvm-objcopy")
    a = Asm()
    top, fwd = a.label(), a.label()
    a.bind(top)
    a.b(fwd)
    a.bl(fwd)
    a.bcc("ne", top)
    a.cbz(1, fwd)
    a.cbz(2, top, wide=False, nz=True)
    a.tbz(3, 3, top, nz=True)
    a.tbz(4, 63, fwd)
    a.adr(2, fwd)
    a.adr(17, top)
    for _ in range(300):
        a.mov(0, 1)
    a.bind(fwd)
    a.b(top)
    a.ret()
    text = (
        "top: b fwd\nbl fwd\nb.ne top\ncbz x1, fwd\ncbnz w2, top\ntbnz w3, #3, top\n"
        "tbz x4, #63, fwd\nadr x2, fwd\nadr x17, top\n" + "mov x0, x1\n" * 300 + "fwd: b top\nret\n"
    )
    obj, raw = tmp_path / "l.o", tmp_path / "l.bin"
    assert LLVM_MC and OBJCOPY
    subprocess.run(
        [LLVM_MC, "-triple=aarch64", "-filetype=obj", "-o", str(obj)],
        input=text,
        text=True,
        check=True,
    )
    subprocess.run(
        [OBJCOPY, "-O", "binary", "--only-section=.text", str(obj), str(raw)], check=True
    )
    assert a.finish() == raw.read_bytes()


def test_adrp_lo12_and_errors() -> None:
    a = Asm()
    t = a.label()
    a.adrp(16, t)
    a.add_lo12(16, 16, t)
    a.buf += bytes(0x2345 - len(a.buf))
    a.bind(t)
    code = a.finish()
    assert code[:8] == (0x90000010 | 2 << 29).to_bytes(4, "little") + bytes.fromhex("10160d91")
    with pytest.raises(ValueError):
        Asm().addsub_imm("add", ZR, 1, 1)  # 31 is sp in add-immediate's Rd
    with pytest.raises(ValueError):
        Asm().addsub("add", SP, 1, 2)  # no sp in shifted-register forms
    with pytest.raises(ValueError):
        Asm().ld(0, 1, 4097 * 8)
    with pytest.raises(ValueError):
        Asm().pair(False, 29, 30, SP, 12)
    with pytest.raises(ValueError):
        Asm().ld(0, 1, 0, 4, signed=True, wide=False)
    b = Asm()
    b.b(b.label())
    with pytest.raises(ValueError, match="never bound"):
        b.finish()
