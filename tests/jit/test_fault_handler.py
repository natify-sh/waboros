# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""The emitted fault handlers off their OS (S0006 step 3): the Darwin offsets against ctypes
mirrors of xnu's headers, both Darwin handlers through llvm-mc (and GNU objdump for x86-64), and
the handler of this machine's architecture called directly with fabricated Linux and Darwin
signal contexts, so the Darwin code paths run on a Linux host. Runs anywhere; the behaviour of
real faults on macOS is test_guard.py's, in CI."""

import ctypes as C
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.jit import native, native_jit, need
from waboros.targets.jit.aarch64 import JitAarch64Target
from waboros.targets.jit.aarch64 import abi as a64
from waboros.targets.jit.common.execmem import load
from waboros.targets.jit.x86_64 import JitX86_64Target
from waboros.targets.jit.x86_64 import abi as x64

U64, U32 = C.c_uint64, C.c_uint32


def _s(*fields: tuple[str, Any]) -> Any:
    return type("S", (C.Structure,), {"_fields_": list(fields)})


# xnu (apple-oss-distributions/xnu, main, read 2026-11-03), LP64, field by field;
# siginfo_t: bsd/sys/signal.h
SIGINFO = _s(*[(n, C.c_int32) for n in "signo errno code pid uid status".split()],
             ("addr", C.c_void_p), ("value", C.c_void_p), ("band", C.c_int64),
             ("pad", U64 * 7))  # fmt: skip
STACK = _s(("sp", C.c_void_p), ("size", U64), ("flags", C.c_int32))  # _sigaltstack.h
UCONTEXT = _s(("onstack", C.c_int32), ("sigmask", U32), ("stack", STACK), ("link", C.c_void_p),
              ("mcsize", U64), ("mcontext", C.c_void_p))  # bsd/sys/_types/_ucontext.h  # fmt: skip
# osfmk/mach/i386/_structs.h
X86_MC = _s(("es", _s(("trapno", C.c_uint16), ("cpu", C.c_uint16), ("err", U32), ("vaddr", U64))),
            ("ss", _s(*[(r, U64) for r in "rax rbx rcx rdx rdi rsi rbp rsp r8 r9 r10 r11 r12 r13 "
                        "r14 r15 rip rflags cs fs gs".split()])))  # fmt: skip
ARM_MC = _s(("es", _s(("far", U64), ("esr", U32), ("exception", U32))),
            ("ss", _s(("x", U64 * 29), ("fp", U64), ("lr", U64), ("sp", U64), ("pc", U64),
                      ("cpsr", U32), ("pad", U32))))  # osfmk/mach/arm/_structs.h  # fmt: skip
SIGACTION = _s(("handler", C.c_void_p), ("mask", U32), ("flags", C.c_int32))  # user struct


def _off(s: Any, path: str) -> int:
    at = 0
    for name in path.split("."):
        at, s = at + getattr(s, name).offset, dict(s._fields_)[name]
    return at


def test_darwin_offsets_match_the_xnu_headers() -> None:
    assert _off(SIGINFO, "code") == x64.SI_CODE == a64.SI_CODE == 8
    assert _off(SIGINFO, "addr") == x64.DARWIN_SI_ADDR == a64.DARWIN_SI_ADDR == 24
    assert _off(UCONTEXT, "mcontext") == x64.DARWIN_UC_MCONTEXT == a64.DARWIN_UC_MCONTEXT == 48
    assert _off(X86_MC, "ss.rip") == x64.DARWIN_MC_RIP == 144
    assert _off(ARM_MC, "ss.pc") == a64.DARWIN_MC_PC == 272
    assert (_off(X86_MC, "es.vaddr"), _off(ARM_MC, "es.far")) == (8, 0)  # = si_addr (sendsig)
    assert C.sizeof(SIGACTION) == x64.DARWIN_SIGACTION == a64.DARWIN_SIGACTION == 16
    assert x64.DARWIN_SIGBUS == a64.DARWIN_SIGBUS == 10


ARGS = (0x7F00_1122_3340, 0x7F00_5566_7780, 0x7F00_99AA_BBC0, 0x7F00_CCDD_EEF0)


def test_the_aarch64_handlers_round_trip_through_llvm_mc() -> None:
    mc = shutil.which("llvm-mc")
    need(mc, "llvm-mc")
    for darwin in (False, True):
        code = JitAarch64Target().fault_handler(*ARGS, darwin=darwin)
        words = " ".join(f"0x{b:02x}" for b in code)
        run = [str(mc), "-triple=aarch64", "-mattr=+bti"]
        out = subprocess.run([*run, "--disassemble"], input=words, capture_output=True, text=True)
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()[:1] not in ("", ".")]
        assert not out.stderr and len(lines) == len(code) // 4, out.stderr
        back = subprocess.run([*run, "-show-encoding"], input="\n".join(lines),
                              capture_output=True, text=True).stdout  # fmt: skip
        encs = re.findall(r"encoding: \[([^\]]*)\]", back)
        assert b"".join(bytes(int(x, 16) for x in e.split(",")) for e in encs) == code
        assert not any(re.search(r"\b[xw]18\b", ln) for ln in lines)  # the platform register
        darwin_only = {"ldr x10, [x2, #48]", "ldr x10, [x10, #272]", "ldr x9, [x1, #24]",
                       "str x13, [x9, #272]", "cmp w0, #10"}  # fmt: skip
        assert darwin_only <= {re.sub(r"\s+", " ", ln) for ln in lines} or not darwin


def test_the_x86_64_handlers_decode_and_reassemble(tmp_path: Path) -> None:
    from tests.jit.test_encoder import LLVM_MC, OBJDUMP, llvm_mc

    need(LLVM_MC and OBJDUMP, "llvm-mc and GNU objdump")
    for darwin in (False, True):
        code = JitX86_64Target().fault_handler(*ARGS, darwin=darwin)
        (tmp_path / "h.bin").write_bytes(code)
        cmd = [str(OBJDUMP), "-D", "-b", "binary", "-m", "i386:x86-64", "-M", "intel"]
        out = subprocess.run([*cmd, "--insn-width=15", str(tmp_path / "h.bin")],
                             capture_output=True, text=True).stdout  # fmt: skip
        insns = [(int(m[0], 16), bytes.fromhex(m[1]), m[2].split("#")[0].strip()) for m in
                 re.findall(r"^\s*([0-9a-f]+):\t([0-9a-f ]+?)\s*\t(.*)$", out, re.M)]  # fmt: skip
        assert b"".join(b for _, b, _ in insns) == code and "(bad)" not in out
        starts = {at for at, _, _ in insns} | {len(code)}
        plain = [(b, t) for _, b, t in insns if not re.match(r"(j\w+|call)\s+0x", t)]
        for at, _, t in insns:  # a relative branch lands on an instruction
            if re.match(r"(j\w+|call)\s+0x", t):
                assert int(t.split()[-1], 16) in starts, (hex(at), t)
        assert [b for b, _ in plain] == llvm_mc([t for _, t in plain])
        darwin_only = {"mov r8,QWORD PTR [rdx+0x30]", "mov r8,QWORD PTR [r8+0x90]",
                       "mov r9,QWORD PTR [rsi+0x18]", "mov QWORD PTR [rax+0x90],r8",
                       "cmp edi,0xa"}  # fmt: skip
        assert darwin_only <= {re.sub(r"\s+", " ", t) for _, t in plain} or not darwin


# a fabricated context: (signal of a guard fault, pc slot, si_addr slot; pc behind a pointer)
LAYOUTS = {
    ("jit-x86_64", False): (11, x64.UC_RIP, x64.SI_ADDR, False),
    ("jit-aarch64", False): (11, a64.UC_PC, a64.SI_ADDR, False),
    ("jit-x86_64", True): (10, _off(X86_MC, "ss.rip"), _off(SIGINFO, "addr"), True),
    ("jit-aarch64", True): (10, _off(ARM_MC, "ss.pc"), _off(SIGINFO, "addr"), True),
}
TARGETS = {"jit-x86_64": JitX86_64Target, "jit-aarch64": JitAarch64Target}
HANDLER = C.CFUNCTYPE(None, C.c_int, C.c_void_p, C.c_void_p)


@pytest.mark.parametrize("darwin", [False, True], ids=["linux", "darwin"])
def test_the_handler_on_fabricated_contexts(darwin: bool) -> None:
    """The handler as a C function: ours (pc rewritten, nothing called), each way of not being
    ours, the saved action chosen by the signal (Darwin: SIGSEGV's, then SIGBUS's), SIG_DFL put
    back with `sigaction` and re-sent with `raise` (Linux: only a sent signal, si_code <= 0)."""
    if sys.platform == "win32":  # no signals; the handler's calls are SysV/AAPCS64 (step 4)
        pytest.skip("no signal handler on Windows")
    jit = native_jit()
    native(jit)
    sig, pc_at, addr_at, indirect = LAYOUTS[jit, darwin]
    area, res = (C.c_uint8 * 4096)(), (C.c_uint8 * 256)()
    lo, rlo = C.addressof(area), C.addressof(res)
    fmap = (U32 * 4)(16, 100, 40, 200)  # fault offset -> resume offset, sorted
    codes = (U64 * 9)(2, 0, 0, 0, 0, lo, lo + 4096, C.addressof(fmap), 2)  # a free entry first
    resv = (U64 * 3)(1, rlo, rlo + 256)
    heads = (U64 * 2)(C.addressof(codes), C.addressof(resv))
    size = 16 if darwin else 152  # the libc's struct sigaction
    prev = (C.c_uint8 * (2 * size))()
    calls: list[tuple[Any, ...]] = []
    keep = [
        C.CFUNCTYPE(C.c_int, C.c_int, C.c_void_p, C.c_void_p)(
            lambda s, a, o: calls.append(("sigaction", s, a, o)) or 0
        ),
        C.CFUNCTYPE(C.c_int, C.c_int)(lambda s: calls.append(("raise", s)) or 0),
        *(HANDLER(lambda s, i, u, k=k: calls.append((f"prev{k}", s))) for k in (0, 1)),
    ]
    entry, reraise, prev0, prev1 = (C.cast(f, C.c_void_p).value or 0 for f in keep)
    code = TARGETS[jit]().fault_handler(
        C.addressof(heads), C.addressof(prev), entry, reraise, darwin=darwin
    )
    mem = load(code)
    handler = HANDLER(mem.addr)
    si, uc, mc = (U64 * 16)(), (U64 * 128)(), (U64 * 128)()
    ctx = mc if indirect else uc
    uc[x64.DARWIN_UC_MCONTEXT // 8] = C.addressof(mc) if indirect else 0

    def fault(s: int, code: int, addr: int, pc: int) -> tuple[int, list[tuple[Any, ...]]]:
        si[0], si[1], si[addr_at // 8] = s, code, addr  # si_signo, si_code at 8, si_addr
        ctx[pc_at // 8] = pc
        calls.clear()
        handler(s, C.addressof(si), C.addressof(uc))
        return ctx[pc_at // 8], list(calls)

    assert fault(sig, 2, rlo + 100, lo + 40) == (lo + 200, [])  # ours
    assert fault(sig, 2, rlo, lo + 16) == (lo + 100, [])
    for p, k in ((prev0, 0), (prev1, 1)):
        C.memmove(C.addressof(prev) + k * size, C.byref(C.c_void_p(p)), 8)
    first = "prev1" if darwin else "prev0"
    for s, code, addr, pc in ((sig, 2, rlo + 100, lo + 41), (sig, 2, rlo + 256, lo + 40),
                              (sig, 0, rlo + 100, lo + 40), (sig, 2, rlo, lo + 4096)):  # fmt: skip
        assert fault(s, code, addr, pc) == (pc, [(first, s)])  # not ours: the saved handler
    if darwin:
        assert fault(11, 2, 0, 0) == (0, [("prev0", 11)])
    C.memset(prev, 0, 2 * size)  # SIG_DFL
    put_back = ("sigaction", sig, C.addressof(prev) + size * (sig == 10), None)
    resend = [("raise", sig)] if darwin else []  # Darwin's si_code tells nothing (LESSONS 57)
    assert fault(sig, 2, 0, 7) == (7, [put_back, *resend])
    assert fault(sig, 0, 0, 7) == (7, [put_back, ("raise", sig)])
    if darwin:  # SIGSEGV's saved action is the first one
        assert fault(11, 2, 0, 7) == (
            7,
            [("sigaction", 11, C.addressof(prev), None), ("raise", 11)],
        )
