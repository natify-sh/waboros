"""AArch64 trampolines (docs/specs/S0004, "Trampolines"): one entry, one bridge, one sp stub, one
`tail_switch` for every OS — AAPCS64 (Linux), Apple arm64 and Windows Arm64 agree on everything
they touch (arguments in x0-x7, result in w0, callee-saved x19-x29, sp, d8-d15); x18 (the platform
register) is never used. All are reached by `blr`/`br x16|x17`, so each starts with `bti c` (R2).

Register roles: x28 context, x27 value-stack frame, x26/x25 memory 0 base/size (one `ldp` from
the context), w0 status. Every trampoline keeps sp 16-byte aligned at all times and pushes a frame
record {x29, x30} (tail_switch: a {x28, x30} restore frame); every `ret` returns to its `bl`/`blr`.
"""

from waboros.targets.jit.aarch64.encoder import FP, LR, SP, ZR, Asm
from waboros.targets.jit.common.context import (
    HOST_BRIDGE,
    HOST_ERROR,
    STATUS_HOST_ERROR,
    TRAP_CODE,
    Layout,
)

CTX, FRAME, MBASE, MSIZE = 28, 27, 26, 25
MEM0 = 8 * Layout().memory(0)  # byte offset of memory 0's base word; its size word follows


def reload_memory(a: Asm) -> None:
    a.pair(True, MBASE, MSIZE, CTX, MEM0)


def entry(a: Asm) -> None:
    """int32_t entry(x0 = ctx, x1 = frame, x2 = code): save x19-x28 under a frame record, set up
    x28/x27/x26/x25, call the code, keep a non-zero status in ctx[TRAP_CODE]. Nothing else is
    stored in the context, so entries nest (LESSONS 4)."""
    done = a.label()
    a.bti("c")
    a.pair(False, FP, LR, SP, -96, "pre")
    a.mov(FP, SP)
    for i, r in enumerate(range(19, 29, 2)):
        a.pair(False, r, r + 1, SP, 16 + 16 * i)
    a.mov(CTX, 0)
    a.mov(FRAME, 1)
    reload_memory(a)
    a.blr(2)
    a.cbz(0, done, wide=False)
    a.st(0, CTX, 8 * TRAP_CODE)  # w0 was written by a 32-bit operation: x0 is zero-extended
    a.bind(done)
    for i, r in enumerate(range(19, 29, 2)):
        a.pair(True, r, r + 1, SP, 16 + 16 * i)
    a.pair(True, FP, LR, SP, 96, "post")
    a.ret()


def bridge(a: Asm) -> None:
    """Wasm code: x0 = context word of the host function, x1 = its slots, then
    `ldr x16, [x28, #8*HOST_BRIDGE]; blr x16`. Calls host(ctx, slots) with ctx[HOST_ERROR] preset
    to 13 and returns the status the host left there in w0 (its C return value is ignored, D34);
    the word's previous value (an enclosing host call's preset) is put back. Saves x2-x8 for every
    frame above (LESSONS 2); x19-x28 are callee-saved for the host. The call site reloads
    x26/x25."""
    status = 8 * HOST_ERROR
    a.bti("c")
    a.pair(False, FP, LR, SP, -80, "pre")
    a.mov(FP, SP)
    a.pair(False, 2, 3, SP, 16)
    a.pair(False, 4, 5, SP, 32)
    a.pair(False, 6, 7, SP, 48)
    a.ld(9, CTX, status)
    a.pair(False, 8, 9, SP, 64)  # x8 and the enclosing preset
    a.mov_imm(9, STATUS_HOST_ERROR)
    a.st(9, CTX, status)
    a.ld_idx(16, CTX, 0, scaled=True)  # x16 = ctx[x0]
    a.mov(0, CTX)  # x1 already holds the slots
    a.blr(16)
    a.ld(0, CTX, status, size=4)  # 32-bit: zero-extended
    a.pair(True, 8, 9, SP, 64)
    a.st(9, CTX, status)
    a.pair(True, 6, 7, SP, 48)
    a.pair(True, 4, 5, SP, 32)
    a.pair(True, 2, 3, SP, 16)
    a.pair(True, FP, LR, SP, 80, "post")
    a.ret()


def sp_stub(a: Asm) -> None:
    """Run through the entry like a wasm function: frame[0] = sp as a wasm function sees it."""
    a.bti("c")
    a.mov(16, SP)
    a.st(16, FRAME)
    a.mov_imm(0, 0, wide=False)
    a.ret()


def tail_switch(a: Asm) -> None:
    """A tail call into a function record whose context is not the caller's (D30). Entered by
    `br x17` after the caller has torn down its frame: x16 = the record, the arguments at x27,
    x30 = the caller's return address (S0004; on x86-64 it is on the stack).

    The first switch of a chain leaves a 16-byte restore frame {x28, x30} and calls the target
    with `blr`, whose return address is `restore`; a later switch whose x30 is already `restore`
    only changes the context and jumps, so a chain of tail calls across contexts stays in
    constant native stack. Every `ret` returns to its `blr` (Arm GCS, R2)."""
    restore, hop = a.label(), a.label()
    a.bti("c")
    a.adr(17, restore)
    a.cmp(LR, 17)
    a.bcc("eq", hop)
    a.pair(False, CTX, LR, SP, -16, "pre")
    a.ld(CTX, 16, 16)
    reload_memory(a)
    a.ld(17, 16)
    a.blr(17)
    a.bind(restore)  # the status stays in w0
    a.pair(True, CTX, LR, SP, 16, "post")
    reload_memory(a)
    a.ret()
    a.bind(hop)
    a.ld(CTX, 16, 16)
    reload_memory(a)
    a.ld(17, 16)
    a.br(17)


def host_thunk(a: Asm, word: int) -> None:
    """The code a host import's function record points to (D32): called like a wasm function
    (frame in x27, context in x28), it calls the import in context word `word` through the
    bridge with its frame as the slots and returns the bridge's status. It reloads x26/x25: as a
    tail-call target it returns to callers that do not (LESSONS 33)."""
    a.bti("c")
    a.pair(False, FP, LR, SP, -16, "pre")
    a.mov(FP, SP)
    a.mov(1, FRAME)
    a.mov_imm(0, word, wide=False)
    a.ld(16, CTX, 8 * HOST_BRIDGE)
    a.blr(16)
    reload_memory(a)
    a.pair(True, FP, LR, SP, 16, "post")
    a.ret()


def assemble() -> tuple[bytes, list[int]]:
    """The four trampolines in one blob and their offsets (entry, bridge, sp stub, tail_switch)."""
    a = Asm()
    offsets: list[int] = []
    for emit in (entry, bridge, sp_stub, tail_switch):
        offsets.append(len(a.buf))
        emit(a)
    return a.finish(), offsets


# The S0006 fault handler, Linux aarch64: the kernel's siginfo_t and ucontext_t (the same for
# glibc, musl and bionic: uc_flags, uc_link, uc_stack (24 bytes), uc_sigmask with its padding to
# 128 bytes, then the 16-aligned `struct sigcontext` {fault_address, regs[31], sp, pc, ...}).
SI_CODE, SI_ADDR = 8, 16  # si_signo, si_errno, si_code, padding, then si_addr
UC_PC = 176 + 8 + 8 * 31 + 8  # uc_mcontext at 176: fault_address, x0-x30, sp, then pc
# macOS arm64 (S0006 step 3), from xnu (apple-oss-distributions/xnu, main, read 2026-11-03):
# siginfo_t and ucontext_t as on x86-64 (x86_64/abi.py: si_addr at 24, uc_mcontext at 48 is a
# pointer). __darwin_mcontext64 (bsd/arm/_mcontext.h): __es (arm_exception_state64,
# osfmk/mach/arm/_structs.h: far, esr, exception; 16 bytes), then __ss (x0-x28, fp, lr, sp, then
# pc). The pc is unsigned in an arm64 (not arm64e) process, which CPython is: its threads run
# with user JOP disabled, so sigreturn takes the state as is (osfmk/arm64/status.c,
# machine_thread_state_convert_from_user). SIGBUS 10; libc's `struct sigaction` is 16 bytes.
DARWIN_SI_ADDR, DARWIN_UC_MCONTEXT, DARWIN_MC_PC = 24, 48, 16 + 8 * 32
DARWIN_SIGBUS, DARWIN_SIGACTION = 10, 16
# Windows on Arm64 (S0006 step 4): EXCEPTION_POINTERS and EXCEPTION_RECORD as on x64
# (x86_64/abi.py, same sources). ARM64_NT_CONTEXT (winnt.h): ContextFlags, Cpsr, X0-X28, Fp, Lr,
# Sp at 0x100, then Pc at 0x108.
WIN_EP_RECORD, WIN_EP_CONTEXT = 0, 8
WIN_ER_CODE, WIN_ER_NPARAMS, WIN_ER_INFO = 0, 24, 32
WIN_CTX_PC = 0x108
WIN_ACCESS_VIOLATION = 0xC0000005


def fault_handler(
    a: Asm,
    tables: int,
    prev: int,
    sigaction: int,
    reraise: int,
    darwin: bool = False,
    windows: bool = False,
) -> None:
    """void handler(int sig, siginfo_t *si, ucontext_t *uc), installed with SA_SIGINFO: the
    contract of x86_64/abi.py `fault_handler` (S0006 "The handler contract") in A64. A fault is
    ours when si_code > 0, the PC is a fault point of a registered code area (binary search of
    its map of u32 pairs) and si_addr lies in a registered reservation: the PC in `uc` becomes
    the resume address and the handler returns. Otherwise the previous action: a handler is
    reached by `br x16` (a `bti c` pad accepts it, R2) with x0-x2, sp and x30 untouched; SIG_DFL
    or SIG_IGN is put back with libc's `sigaction` and, for a sent signal (si_code <= 0), sent
    again with libc's `raise` (LESSONS 46), under a frame record that keeps x30 (the kernel's
    sigreturn) and sp 16-aligned. Only x9-x17 and x29 are written (x0-x2 before those calls);
    x18, the platform register, never; no lock, no allocation. It starts with `bti c`: the
    kernel enters it by a branch that BTI checks (measured under QEMU, S0006); the resume point
    needs no pad (sigreturn is not a branch). Wasmtime's aarch64 Linux path (read, not copied)
    likewise rewrites `pc` in the saved mcontext and returns. `darwin`: as in x86_64/abi.py
    (the pc behind the uc_mcontext pointer, a saved action per signal in x15, always `raise`:
    under SIG_DFL the pending signal ends the process, under SIG_IGN xnu drops it and a fault
    repeats forever, as without us);
    Darwin enforces no BTI on user code, so `bti c` is a no-op there.
    `windows`: the vectored exception handler of x86_64/abi.py in A64, `LONG h(EXCEPTION_POINTERS
    *ep)` with ep in x0; x1 and x2 take the record and the ContextRecord, so the rest is the
    Linux path with Windows offsets; it returns -1 or 0 in w0, calls nothing and writes only
    x0-x2 and x9-x17 (volatile on Windows Arm64), never x18 (the TEB)."""
    notours, scan, skip, search, bisect, below, found, rscan, rskip, ours, dfl, done = (
        a.label() for _ in range(12)
    )
    a.bti("c")
    if windows:
        a.ld(1, 0, WIN_EP_RECORD)
        a.ld(2, 0, WIN_EP_CONTEXT)
        a.ld(9, 1, WIN_ER_CODE, 4)
        a.mov_imm(10, WIN_ACCESS_VIOLATION, wide=False)
        a.cmp(9, 10, wide=False)
        a.bcc("ne", notours)
        a.ld(9, 1, WIN_ER_NPARAMS, 4)
        a.addsub_imm("subs", ZR, 9, 2, False)
        a.bcc("lo", notours)
    else:
        a.ld(9, 1, SI_CODE, 4)
        a.addsub_imm("subs", ZR, 9, 0, False)  # cmp w9, #0: signed
        a.bcc("le", notours)  # kill/raise/sigqueue: never ours (on Linux)
    if windows:
        a.ld(10, 2, WIN_CTX_PC)
    elif darwin:
        a.ld(10, 2, DARWIN_UC_MCONTEXT)
        a.ld(10, 10, DARWIN_MC_PC)
    else:
        a.ld(10, 2, UC_PC)  # x10 = pc
    a.mov_imm(11, tables)
    a.ld(11, 11)
    a.ld(12, 11)  # capacity
    a.addsub_imm("add", 11, 11, 8)
    a.bind(scan)  # the code area holding the pc (a free entry has hi = 0)
    a.cbz(12, notours)
    a.ld(13, 11)  # lo
    a.cmp(10, 13)
    a.bcc("lo", skip)
    a.ld(14, 11, 8)
    a.cmp(10, 14)
    a.bcc("lo", search)
    a.bind(skip)
    a.addsub_imm("add", 11, 11, 32)
    a.addsub_imm("sub", 12, 12, 1)
    a.b(scan)
    a.bind(search)  # x9 = pc - lo, x11 = map; [x12, x14) holds the candidates
    a.addsub("sub", 9, 10, 13)
    a.ld(14, 11, 24)
    a.ld(11, 11, 16)
    a.mov_imm(12, 0)
    a.bind(bisect)
    a.cmp(12, 14)
    a.bcc("hs", notours)  # a pc in our code that is not a fault point: a crash, not a trap
    a.addsub("add", 15, 12, 14)
    a.bfm("ubfm", 15, 15, 1, 63)  # lsr #1
    a.addsub("add", 17, 11, 15, sh="lsl", amount=3)  # the pair
    a.ld(16, 17, 0, 4)  # its fault offset, zero-extended
    a.cmp(9, 16)
    a.bcc("eq", found)
    a.bcc("lo", below)
    a.addsub_imm("add", 12, 15, 1)
    a.b(bisect)
    a.bind(below)
    a.mov(14, 15)
    a.b(bisect)
    a.bind(found)
    a.ld(16, 17, 4, 4)  # the resume offset
    a.addsub("add", 13, 13, 16)  # x13 = code base + resume offset
    a.ld(9, 1, WIN_ER_INFO + 8 if windows else DARWIN_SI_ADDR if darwin else SI_ADDR)
    a.mov_imm(11, tables)
    a.ld(11, 11, 8)
    a.ld(12, 11)
    a.addsub_imm("add", 11, 11, 8)
    a.bind(rscan)  # the fault address in any registered reservation (S0006 Q3)
    a.cbz(12, notours)
    a.ld(14, 11)
    a.cmp(9, 14)
    a.bcc("lo", rskip)
    a.ld(14, 11, 8)
    a.cmp(9, 14)
    a.bcc("lo", ours)
    a.bind(rskip)
    a.addsub_imm("add", 11, 11, 16)
    a.addsub_imm("sub", 12, 12, 1)
    a.b(rscan)
    a.bind(ours)
    if darwin:
        a.ld(9, 2, DARWIN_UC_MCONTEXT)
        a.st(13, 9, DARWIN_MC_PC)
    else:
        a.st(13, 2, WIN_CTX_PC if windows else UC_PC)
    if windows:
        a.mov_imm(0, -1, wide=False)  # EXCEPTION_CONTINUE_EXECUTION
        a.ret()
        a.bind(notours)
        a.mov_imm(0, 0, wide=False)  # EXCEPTION_CONTINUE_SEARCH: the next vectored handler
        a.ret()
        return
    a.ret()
    a.bind(notours)
    if darwin:  # x15 = the saved action of this signal
        segv = a.label()
        a.mov_imm(15, prev)
        a.addsub_imm("subs", ZR, 0, DARWIN_SIGBUS, False)
        a.bcc("ne", segv)
        a.addsub_imm("add", 15, 15, DARWIN_SIGACTION)
        a.bind(segv)
        a.ld(16, 15)
    else:
        a.mov_imm(16, prev)
        a.ld(16, 16)  # its sa_handler / sa_sigaction
    a.addsub_imm("subs", ZR, 16, 1)
    a.bcc("ls", dfl)
    a.br(16)  # x0, x1, x2 as the kernel passed them
    a.bind(dfl)  # sigaction(sig, prev, NULL); w0 is still the signal
    a.pair(False, FP, LR, SP, -32, "pre")
    a.mov(FP, SP)
    a.pair(False, 0, 1, SP, 16)
    if darwin:
        a.mov(1, 15)
    else:
        a.mov_imm(1, prev)
    a.mov_imm(2, 0)
    a.mov_imm(16, sigaction)
    a.blr(16)
    if not darwin:  # Darwin: always re-sent (x86_64/abi.py)
        a.ld(1, SP, 24)
        a.ld(9, 1, SI_CODE, 4)
        a.addsub_imm("subs", ZR, 9, 0, False)
        a.bcc("gt", done)  # a fault: the instruction faults again
    a.ld(0, SP, 16)
    a.mov_imm(16, reraise)
    a.blr(16)  # raise(sig)
    a.bind(done)
    a.pair(True, FP, LR, SP, 32, "post")
    a.ret()
