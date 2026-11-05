# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""The emitted fault handlers off their OS (S0006 steps 3, 4): the Darwin and Windows offsets
against ctypes mirrors of xnu's and the Windows SDK's headers, the Darwin and Windows handlers
through llvm-mc (and GNU objdump for x86-64), and the handler of this machine's architecture
called directly with fabricated Linux, Darwin and Windows contexts, so those code paths run on a
Linux host. Runs anywhere; the behaviour of real faults on macOS and Windows is test_guard.py's,
in CI."""

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
from waboros.targets.jit.common import guard
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
# the Windows SDK's winnt.h (mingw-w64-headers, master e3d18511, read 2026-11-03)
EXC_POINTERS = _s(("record", C.c_void_p), ("context", C.c_void_p))
EXC_RECORD = _s(("code", U32), ("flags", U32), ("record", C.c_void_p), ("address", C.c_void_p),
                ("nparams", U32), ("info", U64 * 15))  # fmt: skip
X64_CONTEXT = _s(*[(f"p{i}home", U64) for i in range(1, 7)], ("flags", U32), ("mxcsr", U32),
                 *[(f"seg{r}", C.c_uint16) for r in "cs ds es fs gs ss".split()], ("eflags", U32),
                 *[(f"dr{i}", U64) for i in (0, 1, 2, 3, 6, 7)],
                 *[(r, U64) for r in "rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 "
                   "r14 r15 rip".split()])  # fmt: skip
ARM64_CONTEXT = _s(("flags", U32), ("cpsr", U32), ("x", U64 * 29), ("fp", U64), ("lr", U64),
                   ("sp", U64), ("pc", U64))  # ARM64_NT_CONTEXT  # fmt: skip


def _off(s: Any, path: str) -> int:
    at = 0
    for name in path.split("."):
        at, s = at + getattr(s, name).offset, dict(s._fields_)[name]
    return at


# The mirrors above describe 64-bit OS ABIs (pointers are 8 bytes); ctypes lays them out with the
# interpreter's own pointer size, so on a 32-bit Python (i686, armv7l) they are not the structures.
only_64bit = pytest.mark.skipif(C.sizeof(C.c_void_p) != 8, reason="the mirrors are 64-bit layouts")


@only_64bit
def test_darwin_offsets_match_the_xnu_headers() -> None:
    assert _off(SIGINFO, "code") == x64.SI_CODE == a64.SI_CODE == 8
    assert _off(SIGINFO, "addr") == x64.DARWIN_SI_ADDR == a64.DARWIN_SI_ADDR == 24
    assert _off(UCONTEXT, "mcontext") == x64.DARWIN_UC_MCONTEXT == a64.DARWIN_UC_MCONTEXT == 48
    assert _off(X86_MC, "ss.rip") == x64.DARWIN_MC_RIP == 144
    assert _off(ARM_MC, "ss.pc") == a64.DARWIN_MC_PC == 272
    assert (_off(X86_MC, "es.vaddr"), _off(ARM_MC, "es.far")) == (8, 0)  # = si_addr (sendsig)
    assert C.sizeof(SIGACTION) == x64.DARWIN_SIGACTION == a64.DARWIN_SIGACTION == 16
    assert x64.DARWIN_SIGBUS == a64.DARWIN_SIGBUS == 10


@only_64bit
def test_windows_offsets_match_the_sdk_headers() -> None:
    ep = (_off(EXC_POINTERS, "record"), _off(EXC_POINTERS, "context"))
    er = (_off(EXC_RECORD, "code"), _off(EXC_RECORD, "nparams"), _off(EXC_RECORD, "info"))
    for m in (x64, a64):
        assert (m.WIN_EP_RECORD, m.WIN_EP_CONTEXT) == ep == (0, 8)
        assert (m.WIN_ER_CODE, m.WIN_ER_NPARAMS, m.WIN_ER_INFO) == er == (0, 24, 32)
        assert m.WIN_ACCESS_VIOLATION == 0xC0000005  # STATUS_ACCESS_VIOLATION
    assert _off(X64_CONTEXT, "rip") == x64.WIN_CTX_RIP == 0xF8
    assert _off(ARM64_CONTEXT, "pc") == a64.WIN_CTX_PC == 0x108


ARGS = (0x7F00_1122_3340, 0x7F00_5566_7780, 0x7F00_99AA_BBC0, 0x7F00_CCDD_EEF0)
OSES = {"linux": {}, "darwin": {"darwin": True}, "windows": {"windows": True}}


def test_the_aarch64_handlers_round_trip_through_llvm_mc() -> None:
    mc = shutil.which("llvm-mc")
    need(mc, "llvm-mc")
    for os_, kw in OSES.items():
        code = JitAarch64Target().fault_handler(*ARGS, **kw)
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
        only = {"darwin": {"ldr x10, [x2, #48]", "ldr x10, [x10, #272]", "ldr x9, [x1, #24]",
                           "str x13, [x9, #272]", "cmp w0, #10"},
                "windows": {"ldr x1, [x0]", "ldr x2, [x0, #8]", "ldr x10, [x2, #264]",
                            "ldr x9, [x1, #40]", "str x13, [x2, #264]", "mov w0, #-1"}}  # fmt: skip
        texts = {re.sub(r"\s+", " ", ln.split("//")[0].strip()) for ln in lines}
        assert only.get(os_, set()) <= texts
        if os_ == "windows":  # writes only x0-x2 and x9-x17 (volatile), never x18 (the TEB)
            for ln in lines:
                op, _, rest = ln.partition("\t")
                if not re.match(r"(cmp|b\.|b$|cbz|str|ret|bti)", op):
                    assert int(re.match(r"[xw](\d+)", rest)[1]) in {0, 1, 2, *range(9, 18)}, ln  # pyright: ignore[reportOptionalSubscript]


def test_the_x86_64_handlers_decode_and_reassemble(tmp_path: Path) -> None:
    from tests.jit.test_encoder import LLVM_MC, OBJDUMP, llvm_mc

    need(LLVM_MC and OBJDUMP, "llvm-mc and GNU objdump")
    for os_, kw in OSES.items():
        code = JitX86_64Target().fault_handler(*ARGS, **kw)
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
        only = {"darwin": {"mov r8,QWORD PTR [rdx+0x30]", "mov r8,QWORD PTR [r8+0x90]",
                           "mov r9,QWORD PTR [rsi+0x18]", "mov QWORD PTR [rax+0x90],r8",
                           "cmp edi,0xa"},
                "windows": {"mov rdx,rcx", "cmp DWORD PTR [rax],0xc0000005",
                            "mov r8,QWORD PTR [r8+0xf8]", "mov r9,QWORD PTR [r9+0x28]",
                            "mov QWORD PTR [rax+0xf8],r8", "mov eax,0xffffffff"}}  # fmt: skip
        texts = [re.sub(r"\s+", " ", t) for _, t in plain]
        assert only.get(os_, set()) <= set(texts)
        if os_ == "windows":  # writes only volatile registers (Win64) and the context's Rip
            for t in texts:
                dst = re.match(r"(?:mov|movabs|lea|add|sub|xor|shr) ([^,]+),", t)
                assert not dst or re.fullmatch(r"[re]?(ax|cx|dx)|r(8|9|10|11)d?|"
                                               r"QWORD PTR \[rax\+0xf8\]", dst[1]), t  # fmt: skip


# a fabricated context: (signal of a guard fault, pc slot, si_addr slot; pc behind a pointer)
LAYOUTS = {
    ("jit-x86_64", False): (11, x64.UC_RIP, x64.SI_ADDR, False),
    ("jit-aarch64", False): (11, a64.UC_PC, a64.SI_ADDR, False),
    ("jit-x86_64", True): (10, _off(X86_MC, "ss.rip"), _off(SIGINFO, "addr"), True),
    ("jit-aarch64", True): (10, _off(ARM_MC, "ss.pc"), _off(SIGINFO, "addr"), True),
}
TARGETS = {"jit-x86_64": JitX86_64Target, "jit-aarch64": JitAarch64Target}
HANDLER = C.CFUNCTYPE(None, C.c_int, C.c_void_p, C.c_void_p)


@pytest.mark.parametrize("os_", ["linux", "darwin", "android"])
def test_the_handler_on_fabricated_contexts(os_: str) -> None:
    """The handler as a C function: ours (pc rewritten, nothing called), each way of not being
    ours, the saved action chosen by the signal (Darwin: SIGSEGV's, then SIGBUS's), SIG_DFL put
    back with `sigaction` and re-sent with `raise` (Linux: only a sent signal, si_code <= 0).
    Android (step 5): the Linux context with bionic's struct sigaction, as guard.py lays it out."""
    darwin = os_ == "darwin"
    if sys.platform == "win32":  # no signals; the handler's calls are SysV/AAPCS64 (step 4)
        pytest.skip("no signal handler on Windows")
    jit = native_jit()
    native(jit)
    if os_ == "android" and jit != "jit-x86_64":
        pytest.skip("Android arm64 has no guard mode (S0006)")
    sigaction = type("S", (C.Structure,), {"_fields_": guard._SIGACTION[os_]})  # pyright: ignore[reportPrivateUsage]
    size, at = C.sizeof(sigaction), sigaction.handler.offset  # pyright: ignore[reportAttributeAccessIssue]
    # glibc/musl, xnu's user struct, bionic LP64 (libc/include/bits/signal_types.h)
    assert (size, at) == {"linux": (152, 0), "darwin": (16, 0), "android": (32, 8)}[os_]
    assert os_ != "android" or at == x64.BIONIC_SA_HANDLER
    sig, pc_at, addr_at, indirect = LAYOUTS[jit, darwin]
    area, res = (C.c_uint8 * 4096)(), (C.c_uint8 * 256)()
    lo, rlo = C.addressof(area), C.addressof(res)
    fmap = (U32 * 4)(16, 100, 40, 200)  # fault offset -> resume offset, sorted
    codes = (U64 * 9)(2, 0, 0, 0, 0, lo, lo + 4096, C.addressof(fmap), 2)  # a free entry first
    resv = (U64 * 3)(1, rlo, rlo + 256)
    heads = (U64 * 2)(C.addressof(codes), C.addressof(resv))
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
        C.addressof(heads), C.addressof(prev), entry, reraise, darwin=darwin,
        bionic=os_ == "android",
    )  # fmt: skip
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
        C.memmove(C.addressof(prev) + k * size + at, C.byref(C.c_void_p(p)), 8)
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


def test_the_windows_handler_on_fabricated_contexts() -> None:
    """S0006 step 4: the vectored handler as a C function, `LONG h(EXCEPTION_POINTERS *)`: ours
    (the PC rewritten, -1) and each way of not being ours (0): another exception code (an in-page
    error), too few parameters, a PC off a fault point or outside the code, an address outside
    the reservations; nothing else is written. Off Windows, x86-64 calls it through a SysV ->
    Win64 thunk (the argument in rcx); aarch64 passes it in x0 everywhere."""
    jit = native_jit()
    native(jit)
    area, res = (C.c_uint8 * 4096)(), (C.c_uint8 * 256)()
    lo, rlo = C.addressof(area), C.addressof(res)
    fmap = (U32 * 4)(16, 100, 40, 200)
    codes = (U64 * 9)(2, 0, 0, 0, 0, lo, lo + 4096, C.addressof(fmap), 2)
    resv = (U64 * 3)(1, rlo, rlo + 256)
    heads = (U64 * 2)(C.addressof(codes), C.addressof(resv))
    mem = load(TARGETS[jit]().fault_handler(C.addressof(heads), 0, 0, 0, windows=True))
    entry = mem.addr
    if jit == "jit-x86_64" and sys.platform != "win32":
        from waboros.targets.jit.x86_64.encoder import RAX, RCX, RDI, Asm

        a = Asm()
        a.endbr64()
        a.mov(RCX, RDI, w=True)
        a.mov_imm(RAX, mem.addr, w=True)
        a.jmp(RAX)
        thunk = load(a.finish())
        entry = thunk.addr
    handler = C.CFUNCTYPE(C.c_int32, C.c_void_p)(entry)
    rec, ctx = (U64 * 20)(), (U64 * 128)()
    ep = (U64 * 2)(C.addressof(rec), C.addressof(ctx))
    pc_at = (x64.WIN_CTX_RIP if jit == "jit-x86_64" else a64.WIN_CTX_PC) // 8

    def rest() -> bytes:
        return bytes(ep) + bytes(rec) + bytes(ctx)[: 8 * pc_at] + bytes(ctx)[8 * pc_at + 8 :]

    def fault(code: int, nparams: int, addr: int, pc: int) -> tuple[int, int]:
        rec[0], rec[3], rec[4], rec[5] = code, nparams, 1, addr  # code; count; write, address
        ctx[pc_at] = pc
        before = rest()
        got = handler(C.addressof(ep))
        assert rest() == before  # nothing but the PC written
        return got, ctx[pc_at]

    av = 0xC0000005
    assert fault(av, 2, rlo + 100, lo + 40) == (-1, lo + 200)  # EXCEPTION_CONTINUE_EXECUTION
    assert fault(av, 2, rlo, lo + 16) == (-1, lo + 100)
    for code, n, addr, pc in ((0xC0000006, 3, rlo + 100, lo + 40), (av, 1, rlo + 100, lo + 40),
                              (av, 2, rlo + 100, lo + 41), (av, 2, rlo + 256, lo + 40),
                              (av, 2, rlo + 100, lo + 4096),
                              (0x80000003, 2, rlo, lo + 40)):  # fmt: skip
        assert fault(code, n, addr, pc) == (0, pc)  # EXCEPTION_CONTINUE_SEARCH
