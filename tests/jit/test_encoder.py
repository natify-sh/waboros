"""The x86-64 encoder, form by form, against `llvm-mc --show-encoding` (S0002 step 2); the ISA-level
check (tools/isa_check.py, D24) on its output. Dev tools; skipped where they are absent."""

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.jit import need
from w2py.targets.jit.x86_64.encoder import ALU, BT, CC, RAX, RBP, RSP, SHIFT, SSE, UNARY, Asm, Mem

LLVM_MC = shutil.which("llvm-mc")
OBJCOPY = shutil.which("llvm-objcopy") or shutil.which("objcopy")


def _gnu_objdump() -> str | None:
    """tools/isa_check.py needs GNU objdump (-b binary) with x86-64; macOS's is llvm-objdump."""
    path = shutil.which("objdump")
    if path is None:
        return None
    out = subprocess.run([path, "--version"], capture_output=True, text=True).stdout
    archs = subprocess.run([path, "--help"], capture_output=True, text=True).stdout
    return path if "GNU objdump" in out and "x86-64" in archs else None  # arm64: native only


OBJDUMP = _gnu_objdump()
ROOT = Path(__file__).resolve().parents[2]

_N = "ax cx dx bx sp bp si di".split()
R64 = ["r" + n for n in _N] + [f"r{i}" for i in range(8, 16)]
R32 = ["e" + n for n in _N] + [f"r{i}d" for i in range(8, 16)]
R16 = _N + [f"r{i}w" for i in range(8, 16)]
R8 = "al cl dl bl spl bpl sil dil".split() + [f"r{i}b" for i in range(8, 16)]
REGS = range(16)
WIDTH = {8: ("byte", R8), 16: ("word", R16), 32: ("dword", R32), 64: ("qword", R64)}

Emit = Callable[[Asm], None]
Form = tuple[Emit, str]


def mem_text(m: Mem, size: int) -> str:
    s = R64[m.base]
    if m.index is not None:
        s += f" + {R64[m.index]}*{m.scale}"
    if m.disp:
        s += f" {'-' if m.disp < 0 else '+'} {abs(m.disp)}"
    return f"{WIDTH[size][0]} ptr [{s}]"


def op_text(x: int | Mem, size: int) -> str:
    return mem_text(x, size) if isinstance(x, Mem) else WIDTH[size][1][x]


DISPS = (0, 8, -128, 127, 128, -0x12345678)
# every base x every index (none, or any but rsp) x every scale x displacement sizes
MEMS_ALL = [
    Mem(b, d, i, s)
    for b in REGS
    for i in [None, *(r for r in REGS if r != RSP)]
    for s in ((1,) if i is None else (1, 2, 4, 8))
    for d in DISPS
]
# every base without index, with 0/8/large displacements, plus a few SIB forms
MEMS = [Mem(b, d) for b in REGS for d in (0, 8, 0x1000)] + [
    Mem(RSP, -128, 12, 4),
    Mem(12, 0x1000, RBP, 8),
    Mem(13, 0, 15, 2),
    Mem(RBP, 0, RAX, 1),
]


def forms() -> list[Form]:
    out: list[Form] = []

    def add(emit: Emit, text: str) -> None:
        out.append((emit, text))

    for w, sz in ((False, 32), (True, 64)):
        r = WIDTH[sz][1]
        for d in REGS:
            for s in REGS:
                add(lambda a, d=d, s=s, w=w: a.mov(d, s, w), f"mov {r[d]}, {r[s]}")
                add(lambda a, d=d, s=s, w=w: a.test(d, s, w), f"test {r[d]}, {r[s]}")
                add(lambda a, d=d, s=s, w=w: a.imul(d, s, w), f"imul {r[d]}, {r[s]}")
                add(lambda a, d=d, s=s, w=w: a.bsf(d, s, w), f"bsf {r[d]}, {r[s]}")
                add(lambda a, d=d, s=s, w=w: a.bsr(d, s, w), f"bsr {r[d]}, {r[s]}")
                add(lambda a, d=d, s=s, w=w: a.cmov("l", d, s, w), f"cmovl {r[d]}, {r[s]}")
                for op in ALU:
                    add(lambda a, d=d, s=s, w=w, op=op: a.alu(op, d, s, w), f"{op} {r[d]}, {r[s]}")
                for op in BT:
                    add(lambda a, d=d, s=s, w=w, op=op: a.bt(op, d, s, w), f"{op} {r[d]}, {r[s]}")
                for ext in (8, 16):
                    t = WIDTH[ext][1][s]
                    add(lambda a, d=d, s=s, w=w, e=ext: a.movzx(d, s, e, w), f"movzx {r[d]}, {t}")
                    add(lambda a, d=d, s=s, w=w, e=ext: a.movsx(d, s, e, w), f"movsx {r[d]}, {t}")
            for m in MEMS_ALL:
                add(lambda a, d=d, m=m, w=w: a.mov(d, m, w), f"mov {r[d]}, {mem_text(m, sz)}")
            for i, m in enumerate(MEMS):
                mt = mem_text(m, sz)
                op = list(ALU)[i % len(ALU)]
                add(lambda a, d=d, m=m, w=w: a.mov(m, d, w), f"mov {mt}, {r[d]}")
                add(lambda a, d=d, m=m, w=w, op=op: a.alu(op, d, m, w), f"{op} {r[d]}, {mt}")
                add(lambda a, d=d, m=m, w=w, op=op: a.alu(op, m, d, w), f"{op} {mt}, {r[d]}")
                add(lambda a, d=d, m=m, w=w: a.test(m, d, w), f"test {mt}, {r[d]}")
                add(lambda a, d=d, m=m, w=w: a.imul(d, m, w), f"imul {r[d]}, {mt}")
                add(lambda a, d=d, m=m, w=w: a.cmov("ae", d, m, w), f"cmovae {r[d]}, {mt}")
                add(lambda a, d=d, m=m, w=w: a.bsr(d, m, w), f"bsr {r[d]}, {mt}")
                add(lambda a, d=d, m=m, w=w: a.lea(d, m, w), f"lea {r[d]}, {mt[mt.index('[') :]}")
                add(lambda a, d=d, m=m, w=w: a.bt("bts", m, d, w), f"bts {mt}, {r[d]}")
                for ext in (8, 16):
                    m2 = mem_text(m, ext)
                    add(lambda a, d=d, m=m, w=w, e=ext: a.movzx(d, m, e, w), f"movzx {r[d]}, {m2}")
                    add(lambda a, d=d, m=m, w=w, e=ext: a.movsx(d, m, e, w), f"movsx {r[d]}, {m2}")
            # immediates: every register and memory operand
            imms = [0, 1, -1, 127, -128, 128, -129, 1000, 0x7FFFFFFF, -0x80000000]
            for x in [d, *MEMS[::7]] if d == RAX else [d]:
                xt = op_text(x, sz)
                # unsigned spellings of the width (Raw) normalise to the same encoding
                for v in imms + ([0xFFFFFFFF] if not w else [(1 << 64) - 1, (1 << 64) - 0x81]):
                    t = signed(v, 64) if w else v
                    for op in ALU:
                        add(
                            lambda a, x=x, v=v, w=w, op=op: a.alu_imm(op, x, v, w),
                            f"{op} {xt}, {t}",
                        )
                    add(lambda a, x=x, v=v, w=w: a.test_imm(x, v, w), f"test {xt}, {t}")
                    add(lambda a, x=x, v=v, w=w: a.mov_imm(x, v, w), mov_imm_text(x, v, sz))
                    add(
                        lambda a, d=d, x=x, v=v, w=w: a.imul_imm(d, x, v, w),
                        f"imul {r[d]}, {xt}, {t}",
                    )
                for op in UNARY:
                    add(lambda a, x=x, w=w, op=op: a.unary(op, x, w), f"{op} {xt}")
                for op in SHIFT:
                    for c in (None, 1, 5, sz - 1):
                        ct = "cl" if c is None else str(c)
                        add(
                            lambda a, x=x, w=w, op=op, c=c: a.shift(op, x, c, w), f"{op} {xt}, {ct}"
                        )
                for op in BT:
                    add(
                        lambda a, x=x, w=w, op=op, n=sz - 3: a.bt_imm(op, x, n, w),
                        f"{op} {xt}, {sz - 3}",
                    )
            for v in (0x123456789, -0x80000001, 1 << 63, (1 << 64) - 1):
                if w:
                    add(lambda a, d=d, v=v: a.mov_imm(d, v, True), mov_imm_text(d, v, 64))
        for m in MEMS:
            add(lambda a, m=m, w=w: a.mov_imm(m, -2, w), f"mov {mem_text(m, sz)}, -2")
            add(lambda a, m=m, w=w: a.unary("idiv", m, w), f"idiv {mem_text(m, sz)}")
            add(lambda a, m=m, w=w: a.shift("sar", m, None, w), f"sar {mem_text(m, sz)}, cl")
            add(lambda a, m=m, w=w: a.bt_imm("btr", m, 7, w), f"btr {mem_text(m, sz)}, 7")
    for x in REGS:
        add(lambda a, x=x: a.push(x), f"push {R64[x]}")
        add(lambda a, x=x: a.pop(x), f"pop {R64[x]}")
        add(lambda a, x=x: a.call(x), f"call {R64[x]}")
        add(lambda a, x=x: a.jmp(x), f"jmp {R64[x]}")
        for cc in CC:
            add(lambda a, x=x, cc=cc: a.setcc(cc, x), f"set{cc} {R8[x]}")
            add(
                lambda a, x=x, cc=cc: a.cmov(cc, x, 15 - x, True),
                f"cmov{cc} {R64[x]}, {R64[15 - x]}",
            )
        for m in MEMS:
            add(lambda a, x=x, m=m: a.store8(m, x), f"mov {mem_text(m, 8)}, {R8[x]}")
            add(lambda a, x=x, m=m: a.store16(m, x), f"mov {mem_text(m, 16)}, {R16[x]}")
            add(lambda a, x=x, m=m: a.movsx(x, m, 32), f"movsxd {R64[x]}, {mem_text(m, 32)}")
        for s in REGS:
            add(lambda a, x=x, s=s: a.movsx(x, s, 32), f"movsxd {R64[x]}, {R32[s]}")
    # SSE/SSE2 scalar: every xmm against every register, a few memory operands
    for d in REGS:
        for s in REGS:
            for dbl, sfx in ((False, "ss"), (True, "sd")):
                for op in SSE:
                    t = f"{op}ps" if op in ("and", "or", "xor") else f"{op}{sfx}"
                    add(
                        lambda a, d=d, s=s, op=op, dbl=dbl: a.sse(op, d, s, dbl),
                        f"{t} xmm{d}, xmm{s}",
                    )
                add(lambda a, d=d, s=s, dbl=dbl: a.ucomis(d, s, dbl), f"ucomi{sfx} xmm{d}, xmm{s}")
                add(lambda a, d=d, s=s, dbl=dbl: a.cvtff(d, s, dbl),
                    f"cvt{sfx}2{'ss' if dbl else 'sd'} xmm{d}, xmm{s}")  # fmt: skip
                for w, r in ((False, R32), (True, R64)):
                    add(lambda a, d=d, s=s, dbl=dbl, w=w: a.cvtsi2f(d, s, dbl, w),
                        f"cvtsi2{sfx} xmm{d}, {r[s]}")  # fmt: skip
                    add(lambda a, d=d, s=s, dbl=dbl, w=w: a.cvttf2si(d, s, dbl, w),
                        f"cvtt{sfx}2si {r[d]}, xmm{s}")  # fmt: skip
            for w, r, mv in ((False, R32, "movd"), (True, R64, "movq")):
                add(lambda a, d=d, s=s, w=w: a.movq_to_xmm(d, s, w), f"{mv} xmm{d}, {r[s]}")
                add(lambda a, d=d, s=s, w=w: a.movq_from_xmm(d, s, w), f"{mv} {r[d]}, xmm{s}")
        for m in MEMS:
            add(lambda a, d=d, m=m: a.movs(d, m, False), f"movss xmm{d}, {mem_text(m, 32)}")
            add(lambda a, d=d, m=m: a.movs(d, m, True), f"movsd xmm{d}, {mem_text(m, 64)}")
            add(lambda a, d=d, m=m: a.sse("add", d, m, True), f"addsd xmm{d}, {mem_text(m, 64)}")
            add(lambda a, d=d, m=m: a.ucomis(d, m, False), f"ucomiss xmm{d}, {mem_text(m, 32)}")
    for m in MEMS:
        add(lambda a, m=m: a.call(m), f"call {mem_text(m, 64)}")
        add(lambda a, m=m: a.jmp(m), f"jmp {mem_text(m, 64)}")
    add(lambda a: a.cdq(), "cdq")
    add(lambda a: a.cqo(), "cqo")
    add(lambda a: a.ret(), "ret")
    add(lambda a: a.endbr64(), "endbr64")
    return out


def signed(v: int, sz: int) -> int:
    return v - (1 << sz) if v >= 1 << sz - 1 else v


def mov_imm_text(x: int | Mem, v: int, sz: int) -> str:
    v = signed(v, sz)
    if isinstance(x, Mem):
        return f"mov {mem_text(x, sz)}, {v}"
    if sz == 32 or 0 <= v < 1 << 32:
        return f"mov {R32[x]}, {v & 0xFFFFFFFF}"
    if -(1 << 31) <= v < 1 << 31:
        return f"mov {R64[x]}, {v}"
    return f"movabs {R64[x]}, {v}"


def encode(emit: Emit) -> bytes:
    a = Asm()
    emit(a)
    return a.finish()


def llvm_mc(lines: list[str]) -> list[bytes]:
    assert LLVM_MC
    src = ".intel_syntax noprefix\n" + "\n".join(lines) + "\n"
    p = subprocess.run(
        [LLVM_MC, "--triple=x86_64", "--show-encoding"], input=src, capture_output=True, text=True
    )
    assert p.returncode == 0 and not p.stderr, p.stderr[:2000]
    encs = re.findall(r"encoding: \[([^\]]*)\]", p.stdout)
    return [bytes(int(b, 16) for b in e.split(",")) for e in encs]


def test_forms_match_llvm_mc() -> None:
    need(LLVM_MC, "llvm-mc")
    fs = forms()
    ours = [encode(e) for e, _ in fs]
    theirs = llvm_mc([t for _, t in fs])
    assert len(theirs) == len(fs)
    bad = [
        f"{t}: ours {o.hex(' ')} llvm-mc {r.hex(' ')}"
        for (_, t), o, r in zip(fs, ours, theirs, strict=True)
        if o != r
    ]
    assert not bad, f"{len(bad)} of {len(fs)} forms differ:\n" + "\n".join(bad[:30])
    print(f"{len(fs)} encoder forms checked against llvm-mc")


def label_program() -> tuple[bytes, str]:
    """Forward/backward rel8 and rel32 jumps, calls and rip-relative leas through labels."""
    a = Asm()
    top, near, far, self_, data = a.label(), a.label(), a.label(), a.label(), a.label()
    a.bind(top)
    a.jmp(near, short=True)
    a.jcc("e", near, short=True)
    a.jmp(far)
    a.jcc("ne", far)
    a.call(far)
    a.bind(near)
    for _ in range(80):
        a.mov(RAX, 1)
    a.bind(far)
    a.jmp(top)
    a.jcc("l", top)
    a.bind(self_)
    a.jmp(self_, short=True)
    a.jcc("g", self_, short=True)
    a.lea_label(RAX, top)
    a.lea_label(11, data)
    a.bind(data)
    a.ret()
    text = (
        """top: jmp near
je near
jmp far
jne far
call far
near:
"""
        + "mov eax, ecx\n" * 80
        + """far: jmp top
jl top
self: jmp self
jg self
lea rax, [rip + top]
lea r11, [rip + data]
data: ret
"""
    )
    return a.finish(), text


def test_labels_match_llvm_mc(tmp_path: Path) -> None:
    need(LLVM_MC and OBJCOPY, "llvm-mc/objcopy")
    ours, text = label_program()
    obj, raw = tmp_path / "l.o", tmp_path / "l.bin"
    assert LLVM_MC and OBJCOPY
    subprocess.run(
        [LLVM_MC, "--triple=x86_64", "-filetype=obj", "-o", str(obj)],
        input=".intel_syntax noprefix\n" + text,
        text=True,
        check=True,
    )
    subprocess.run(
        [OBJCOPY, "-O", "binary", "--only-section=.text", str(obj), str(raw)], check=True
    )
    assert ours == raw.read_bytes()


def test_encoder_errors() -> None:
    a = Asm()
    lbl = a.label()
    a.jmp(lbl, short=True)
    for _ in range(64):
        a.mov(RAX, 1)
    a.bind(lbl)
    with pytest.raises(ValueError, match="rel8"):
        a.finish()
    with pytest.raises(ValueError, match="never bound"):
        b = Asm()
        b.jmp(b.label())
        b.finish()
    with pytest.raises(ValueError):
        Mem(RAX, 0, RSP)
    bad: list[Callable[[Asm], None]] = [
        lambda a: a.alu_imm("add", RAX, 0xFFFFFFFF, w=True),  # not a sign-extended imm32
        lambda a: a.alu_imm("add", RAX, 1 << 32),
        lambda a: a.mov_imm(RAX, -(1 << 31) - 1),
        lambda a: a.mov_imm(RAX, 1 << 64, w=True),
        lambda a: a.mov_imm(RAX, -(1 << 63) - 1, w=True),
        lambda a: a.shift("shl", RAX, 32),
        lambda a: a.shift("shl", RAX, 64, w=True),
        lambda a: a.shift("shl", RAX, -1),
        lambda a: a.bt_imm("bt", RAX, 32),
        lambda a: a.mov(16, RAX),
        lambda a: a.push(-1),
        lambda a: a.lea(RAX, Mem(16)),
        lambda a: a.lea(RAX, Mem(RAX, 0, 16)),
    ]
    for emit in bad:
        with pytest.raises(ValueError):
            emit(Asm())
    far = Asm()
    lbl = far.label()
    far.jmp(lbl)
    far.bind(lbl)
    far._labels[lbl.id] = 1 << 32  # pyright: ignore[reportPrivateUsage]  (4 GiB away)
    with pytest.raises(ValueError, match="rel32"):
        far.finish()


def run_isa_check(tmp_path: Path, blob: bytes, *extra: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "blob.bin"
    path.write_bytes(blob)
    cmd = [sys.executable, str(ROOT / "tools" / "isa_check.py"), str(path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


ROUNDSD = bytes.fromhex("660f3a0bc103")  # roundsd xmm0, xmm1, 3 (SSE4.1: x86-64-v2)


def test_isa_check(tmp_path: Path) -> None:
    need(OBJDUMP, "GNU objdump")
    blob = b"".join(encode(e) for e, _ in forms()) + label_program()[0]
    p = run_isa_check(tmp_path, blob)
    assert p.returncode == 0, p.stdout[:2000] + p.stderr
    p = run_isa_check(tmp_path, encode(lambda a: a.ret()) + ROUNDSD + encode(lambda a: a.ret()))
    assert p.returncode == 1 and "roundsd" in p.stdout, p.stdout + p.stderr
    p = run_isa_check(tmp_path, b"\xc3" + ROUNDSD + b"\xc3", "--data", "1:7")  # a data range
    assert p.returncode == 0, p.stdout + p.stderr
    # APX (above any declared level): REX2 `add r16, rax`; EVEX NDD `add rax, rax, rax`
    for apx in ("d51801c0", "62f4fc1801c0"):
        p = run_isa_check(tmp_path, bytes.fromhex(apx))
        assert p.returncode == 1, (apx, p.stdout + p.stderr)
    assert run_isa_check(tmp_path, b"").returncode == 2  # nothing disassembled is not a pass


def test_isa_levels(tmp_path: Path) -> None:
    """D24: an instruction above the baseline fails at v1 and passes once its level is declared;
    VEX is above both."""
    need(OBJDUMP, "GNU objdump")
    popcnt = bytes.fromhex("f30fb8c1")  # popcnt eax, ecx (x86-64-v2)
    vex = bytes.fromhex("c5f958c1")  # vaddpd xmm0, xmm0, xmm1 (AVX)
    for blob, name in ((ROUNDSD, "roundsd"), (popcnt, "popcnt")):
        p = run_isa_check(tmp_path, blob)
        assert p.returncode == 1 and name in p.stdout, p.stdout + p.stderr
        p = run_isa_check(tmp_path, blob, "--level", "v2")
        assert p.returncode == 0, p.stdout + p.stderr
    for level in ("v1", "v2"):
        assert run_isa_check(tmp_path, vex, "--level", level).returncode == 1
