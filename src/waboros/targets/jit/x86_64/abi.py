"""x86-64 trampolines (docs/specs/S0003): the entry and the host bridge for each host calling
convention (SysV; Win64 per D23), and the stack-pointer stub. All are reached through a pointer, so
each starts with `endbr64` (CET IBT, R2); every `ret` matches its `call` (CET shadow stacks).

Register roles: r15 context, r12 value-stack frame, r13/r14 memory 0 base/size, eax status.
Alignment (LESSONS 3): rsp is 8 mod 16 on entry to any of these; each pads to 0 mod 16 before its
`call`, so wasm code always starts with rsp = 8 mod 16, as a C function does.
"""

from waboros.targets.jit.common.context import HOST_ERROR, STATUS_HOST_ERROR, TRAP_CODE, Layout
from waboros.targets.jit.x86_64.encoder import (
    R8,
    R9,
    R10,
    R11,
    R12,
    R13,
    R14,
    R15,
    RAX,
    RBP,
    RBX,
    RCX,
    RDI,
    RDX,
    RSI,
    RSP,
    Asm,
    Mem,
)

MEM0 = 8 * Layout().memory(0)  # byte offset of memory 0's base word; its size word follows


def entry(a: Asm, win64: bool) -> None:
    """int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code): save the registers the host
    convention calls callee-saved, set up r15/r12/r13/r14, call the code, keep a non-zero status
    in ctx[TRAP_CODE]. Nothing else is stored in the context, so entries nest (LESSONS 4)."""
    saved = [RBX, RBP, *((RSI, RDI) if win64 else ()), R12, R13, R14, R15]
    ctx, frame, code = (RCX, RDX, R8) if win64 else (RDI, RSI, RDX)
    a.endbr64()
    for r in saved:
        a.push(r)
    a.alu_imm("sub", RSP, 8, w=True)  # 8 (return address) + 8 * even + 8 = 0 mod 16
    a.mov(R15, ctx, w=True)
    a.mov(R12, frame, w=True)
    a.mov(R13, Mem(R15, MEM0), w=True)
    a.mov(R14, Mem(R15, MEM0 + 8), w=True)
    a.call(code)
    a.mov(RAX, RAX)  # the status is 32-bit; a C host may leave the upper half undefined
    a.test(RAX, RAX)
    done = a.label()
    a.jcc("e", done, short=True)
    a.mov(Mem(R15, 8 * TRAP_CODE), RAX, w=True)
    a.bind(done)
    a.alu_imm("add", RSP, 8, w=True)
    for r in reversed(saved):
        a.pop(r)
    a.ret()


def bridge(a: Asm, win64: bool) -> None:
    """Wasm code: eax = context word of the host function, rdx = its slots, then
    `call [r15 + 8 * HOST_BRIDGE]`. Calls host(ctx, slots) with ctx[HOST_ERROR] preset to 13 and
    returns the status the host left there in eax (its C return value is ignored: ctypes leaves
    it undefined when an exception escapes a callback, D34); the word's previous value is put
    back, so an outer host call still reads its own preset. Saves every pool register the
    convention lets the host clobber, for all frames above (LESSONS 2); the call site reloads
    r13/r14. Win64 also reserves the 32-byte shadow space."""
    saved = [R8, R9, R10, R11] if win64 else [RSI, RDI, R8, R9, R10, R11]
    pad = (32 if win64 else 0) + 8 * (len(saved) % 2)  # 8 + 8 * (saved + 1) + pad = 0 mod 16
    status = Mem(R15, 8 * HOST_ERROR)
    a.endbr64()
    for r in saved:
        a.push(r)
    a.mov(RCX, status, w=True)
    a.push(RCX)  # the enclosing host call's preset (nested entries share contexts)
    if pad:
        a.alu_imm("sub", RSP, pad, w=True)
    a.mov_imm(status, STATUS_HOST_ERROR, w=True)
    if win64:
        a.mov(RCX, R15, w=True)  # rdx already holds the slots
    else:
        a.mov(RDI, R15, w=True)
        a.mov(RSI, RDX, w=True)
    a.call(Mem(R15, 0, RAX, 8))
    if pad:
        a.alu_imm("add", RSP, pad, w=True)
    a.mov(RAX, status)  # 32-bit: zero-extended
    a.pop(RCX)
    a.mov(status, RCX, w=True)
    for r in reversed(saved):
        a.pop(r)
    a.ret()


def sp_stub(a: Asm) -> None:
    """Run through the entry like a wasm function: frame[0] = rsp as a wasm function sees it."""
    a.endbr64()
    a.mov(Mem(R12), RSP, w=True)
    a.alu("xor", RAX, RAX)
    a.ret()


def tail_switch(a: Asm) -> None:
    """A tail call into a function record whose context is not the caller's (D30). Entered by
    `jmp [r15 + 8 * TAIL_SWITCH]` after the caller has torn down its frame: rdx = the record,
    the arguments at r12, [rsp] = the caller's return address (rsp = 8 mod 16).

    Only `call` sites into the same instance rely on r15 (and r13/r14) surviving a call, so the
    first switch of a chain leaves a restore frame: the context to return to, then a real `call`
    whose return address is `restore`. A later switch whose return address is already `restore`
    only changes the context and jumps: a chain A -> B -> A -> ... of tail calls stays in
    constant native stack (Wasmtime: tail calls never grow the stack). Every `ret` still returns
    to the `call` that pushed its address, so CET shadow stacks stay balanced (R2)."""
    restore = a.label()
    hop = a.label()
    a.endbr64()
    a.lea_label(RCX, restore)
    a.alu("cmp", RCX, Mem(RSP), w=True)
    a.jcc("e", hop, short=True)
    a.push(R15)  # rsp = 0 mod 16: the callee sees 8 mod 16, as after any call
    a.mov(R15, Mem(RDX, 16), w=True)
    a.mov(R13, Mem(R15, MEM0), w=True)
    a.mov(R14, Mem(R15, MEM0 + 8), w=True)
    a.call(Mem(RDX))
    a.bind(restore)  # the status stays in eax
    a.pop(R15)
    a.mov(R13, Mem(R15, MEM0), w=True)
    a.mov(R14, Mem(R15, MEM0 + 8), w=True)
    a.ret()
    a.bind(hop)
    a.mov(R15, Mem(RDX, 16), w=True)
    a.mov(R13, Mem(R15, MEM0), w=True)
    a.mov(R14, Mem(R15, MEM0 + 8), w=True)
    a.jmp(Mem(RDX))


def assemble(win64: bool) -> tuple[bytes, list[int]]:
    """The four trampolines for one convention in one blob and their offsets (entry, bridge, sp
    stub, tail_switch). Both conventions assemble everywhere (the encoder tests see both); loading
    the blob is the host's (common/instance.py), so this module stays portable."""
    a = Asm()
    offsets: list[int] = []
    for emit in (
        lambda: entry(a, win64),
        lambda: bridge(a, win64),
        lambda: sp_stub(a),
        lambda: tail_switch(a),
    ):
        offsets.append(len(a.buf))
        emit()
    return a.finish(), offsets


# The S0006 fault handler, Linux x86-64: the kernel's siginfo_t and ucontext_t (the same for
# glibc, musl and bionic); the saved `struct sigaction` is the libc's (glibc's and musl's agree:
# sa_handler first).
SI_CODE, SI_ADDR = 8, 16  # si_signo, si_errno, si_code, padding, then si_addr
UC_RIP = 40 + 8 * 16  # uc_flags, uc_link, uc_stack (24 bytes), then mcontext gregs[REG_RIP = 16]
# macOS x86-64 (S0006 step 3), from xnu (apple-oss-distributions/xnu, main, read 2026-11-03):
# siginfo_t (bsd/sys/signal.h): si_signo, si_errno, si_code, si_pid, si_uid, si_status, si_addr
# at 24. ucontext_t (bsd/sys/_types/_ucontext.h): uc_onstack, uc_sigmask (u32), uc_stack (24
# bytes), uc_link, uc_mcsize, then uc_mcontext at 48: a pointer to the mcontext, which sigreturn
# reads back through it (bsd/dev/i386/unix_signal.c). __darwin_mcontext64 (bsd/i386/_mcontext.h):
# __es (x86_exception_state64, osfmk/mach/i386/_structs.h: trapno, cpu, err, faultvaddr; 16
# bytes), then __ss (rax rbx rcx rdx rdi rsi rbp rsp r8-r15, then rip). SIGBUS is 10 there, and
# libc's `struct sigaction` is {handler, u32 mask, int flags}: 16 bytes.
DARWIN_SI_ADDR, DARWIN_UC_MCONTEXT, DARWIN_MC_RIP = 24, 48, 16 + 8 * 16
DARWIN_SIGBUS, DARWIN_SIGACTION = 10, 16


def fault_handler(
    a: Asm, tables: int, prev: int, sigaction: int, reraise: int, darwin: bool = False
) -> None:
    """void handler(int sig, siginfo_t *si, ucontext_t *uc), installed with SA_SIGINFO (S0006,
    "The handler contract"). `tables`: two words, the addresses of the registry's code table
    `[capacity, {lo, hi, map, count}...]` and reservation table `[capacity, {lo, hi}...]`; a
    `map` is `count` pairs of u32 `{fault offset, resume offset}`, sorted. A fault is ours when
    si_code > 0 (kernel-generated), the PC is a fault point of a registered code area (binary
    search) and si_addr lies in a registered reservation: the PC in `uc` becomes the resume
    address and the handler returns. Otherwise the previous action (`prev`, a libc struct
    sigaction) takes over: a handler is jumped to with the three arguments untouched; SIG_DFL or
    SIG_IGN is put back with libc's `sigaction` and the handler returns, so the instruction
    faults again under it; a signal that was sent (si_code <= 0) does not recur by itself, so it
    is sent again with libc's `raise` (`reraise`), pending until the handler returns. Only rax,
    rcx and r8-r11 are written; no lock, no allocation.
    `darwin` (macOS, S0006 step 3): the Darwin offsets above, the PC behind the uc_mcontext
    pointer; `prev` is an array of two saved actions, SIGSEGV's then SIGBUS's (PROT_NONE faults
    arrive as SIGBUS, xnu bsd/uxkern/ux_exception.c), chosen by the signal. xnu's sendsig fills
    si_code of every SIGSEGV/SIGBUS, a sent one included (SIGBUS always BUS_ADRERR; SIGSEGV from
    the thread's last exception code), so si_code > 0 does not mean a fault there: "ours" rests
    on the PC and the address, and SIG_DFL/SIG_IGN is always followed by `raise`. Under SIG_DFL
    a fault then ends the process from the pending signal before the instruction runs again,
    with the same signal; under SIG_IGN xnu drops both and the fault repeats forever, as it does
    without us (LESSONS 57).
    The idea of resuming at the module's trap stub and of chaining is poc/w2jit_guard.py's;
    "ours by PC and fault address" is Wasmtime's `test_if_trap` (read, not copied)."""
    notours, scan, skip, search, bisect, below, found, rscan, rskip, ours, dfl, done = (
        a.label() for _ in range(12)
    )
    a.endbr64()  # entered indirectly by the kernel (CET IBT)
    a.alu_imm("cmp", Mem(RSI, SI_CODE), 0)
    a.jcc("le", notours)  # kill/raise/sigqueue: never ours (on Linux)
    if darwin:
        a.mov(R8, Mem(RDX, DARWIN_UC_MCONTEXT), w=True)
        a.mov(R8, Mem(R8, DARWIN_MC_RIP), w=True)
    else:
        a.mov(R8, Mem(RDX, UC_RIP), w=True)
    a.mov_imm(R10, tables, w=True)
    a.mov(R10, Mem(R10), w=True)
    a.mov(R11, Mem(R10), w=True)  # capacity
    a.alu_imm("add", R10, 8, w=True)
    a.bind(scan)  # the code area holding the PC (a free entry has hi = 0)
    a.test(R11, R11, w=True)
    a.jcc("e", notours)
    a.alu("cmp", R8, Mem(R10), w=True)
    a.jcc("b", skip)
    a.alu("cmp", R8, Mem(R10, 8), w=True)
    a.jcc("b", search)
    a.bind(skip)
    a.alu_imm("add", R10, 32, w=True)
    a.alu_imm("sub", R11, 1, w=True)
    a.jmp(scan)
    a.bind(search)  # rax = PC - lo, r10 = map, r11 = count; [rcx, r11) holds the candidates
    a.mov(RAX, R8, w=True)
    a.alu("sub", RAX, Mem(R10), w=True)
    a.mov(R11, Mem(R10, 24), w=True)
    a.mov(R10, Mem(R10, 16), w=True)
    a.alu("xor", RCX, RCX)
    a.bind(bisect)
    a.alu("cmp", RCX, R11, w=True)
    a.jcc("ae", notours)  # a PC in our code that is not a fault point: a crash, not a trap
    a.lea(R9, Mem(RCX, 0, R11))
    a.shift("shr", R9, 1, w=True)
    a.alu("cmp", RAX, Mem(R10, 0, R9, 8))
    a.jcc("e", found)
    a.jcc("b", below)
    a.lea(RCX, Mem(R9, 1))
    a.jmp(bisect)
    a.bind(below)
    a.mov(R11, R9, w=True)
    a.jmp(bisect)
    a.bind(found)
    a.mov(RCX, Mem(R10, 4, R9, 8))  # the resume offset, zero-extended
    a.alu("sub", R8, RAX, w=True)
    a.alu("add", R8, RCX, w=True)  # r8 = code base + resume offset
    a.mov(R9, Mem(RSI, DARWIN_SI_ADDR if darwin else SI_ADDR), w=True)
    a.mov_imm(R10, tables, w=True)
    a.mov(R10, Mem(R10, 8), w=True)
    a.mov(R11, Mem(R10), w=True)
    a.alu_imm("add", R10, 8, w=True)
    a.bind(rscan)  # the fault address in any registered reservation (S0006 Q3)
    a.test(R11, R11, w=True)
    a.jcc("e", notours)
    a.alu("cmp", R9, Mem(R10), w=True)
    a.jcc("b", rskip)
    a.alu("cmp", R9, Mem(R10, 8), w=True)
    a.jcc("b", ours)
    a.bind(rskip)
    a.alu_imm("add", R10, 16, w=True)
    a.alu_imm("sub", R11, 1, w=True)
    a.jmp(rscan)
    a.bind(ours)
    if darwin:
        a.mov(RAX, Mem(RDX, DARWIN_UC_MCONTEXT), w=True)
        a.mov(Mem(RAX, DARWIN_MC_RIP), R8, w=True)
    else:
        a.mov(Mem(RDX, UC_RIP), R8, w=True)
    a.ret()
    a.bind(notours)
    if darwin:  # rcx = the saved action of this signal
        segv = a.label()
        a.mov_imm(RCX, prev, w=True)
        a.alu_imm("cmp", RDI, DARWIN_SIGBUS)
        a.jcc("ne", segv, short=True)
        a.alu_imm("add", RCX, DARWIN_SIGACTION, w=True)
        a.bind(segv)
        a.mov(RAX, Mem(RCX), w=True)
    else:
        a.mov_imm(RAX, prev, w=True)
        a.mov(RAX, Mem(RAX), w=True)  # its sa_handler / sa_sigaction
    a.alu_imm("cmp", RAX, 1, w=True)
    a.jcc("be", dfl)
    a.jmp(RAX)  # rdi, rsi, rdx as the kernel passed them
    a.bind(dfl)  # sigaction(sig, prev, NULL); rdi is still the signal
    a.push(RDI)
    a.push(RSI)
    a.alu_imm("sub", RSP, 8, w=True)  # rsp = 8 mod 16 on entry: aligned for the calls
    if darwin:
        a.mov(RSI, RCX, w=True)
    else:
        a.mov_imm(RSI, prev, w=True)
    a.alu("xor", RDX, RDX)
    a.mov_imm(RAX, sigaction, w=True)
    a.call(RAX)
    if not darwin:  # Darwin: always re-sent (above)
        a.mov(RSI, Mem(RSP, 8), w=True)
        a.alu_imm("cmp", Mem(RSI, SI_CODE), 0)
        a.jcc("g", done)  # a fault: the instruction faults again
    a.mov(RDI, Mem(RSP, 16), w=True)
    a.mov_imm(RAX, reraise, w=True)
    a.call(RAX)  # raise(sig)
    a.bind(done)
    a.alu_imm("add", RSP, 24, w=True)
    a.ret()
