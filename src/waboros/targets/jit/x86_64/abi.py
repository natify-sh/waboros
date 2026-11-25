"""x86-64 trampolines private to this compiler (docs/specs/S0003, S0015): the host bridge for each
host calling convention (SysV; Win64 per D23) and tail_switch; the runtime's entry and
stack-pointer stub are in entry.py. Both are reached through a pointer, so each starts with
`endbr64` (CET IBT, R2); every `ret` matches its `call` (CET shadow stacks).

Register roles: r15 context, r12 value-stack frame, r13/r14 memory 0 base/size, eax status.
Alignment (LESSONS 3): rsp is 8 mod 16 on entry to any of these; each pads to 0 mod 16 before its
`call`, so wasm code always starts with rsp = 8 mod 16, as a C function does.
"""

from waboros.targets.jit.common.context import (
    EXIT_RECORD,
    HOST_BRIDGE,
    HOST_ERROR,
    STATUS_HOST_ERROR,
    Layout,
)
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
    RCX,
    RDI,
    RDX,
    RSI,
    RSP,
    Asm,
    Mem,
)

POOL_VOLATILE = (RSI, RDI, R8, R9, R10, R11)  # the pool registers SysV lets a callee clobber
MEM0 = 8 * Layout().memory(0)  # byte offset of memory 0's base word; its size word follows
EXIT = 8 * EXIT_RECORD  # the store's exit record {sp, pc, frame} (S0013 3a)
# tail_switch's restore frame, from rsp at its call (S0015 "Tail calls across the boundary"):
# the caller's r15 and the six pool registers a C entry may clobber, then the real return address
RESTORE_RET_AT, RESTORE_UP = 56, 64


def bridge(a: Asm, win64: bool) -> None:
    """Wasm code: eax = context word of the host function, rdx = its slots, then
    `call [r15 + 8 * HOST_BRIDGE]`. Calls host(ctx, slots, word) (D75: the word in edx, Win64
    r8d, caller-saved in both) with ctx[HOST_ERROR] preset to 13 and
    returns the status the host left there in eax (its C return value is ignored: ctypes leaves
    it undefined when an exception escapes a callback, D34); the word's previous value is put
    back, so an outer host call still reads its own preset. Saves every pool register the
    convention lets the host clobber, for all frames above (LESSONS 2); the call site reloads
    r13/r14. Win64 also reserves the 32-byte shadow space. Before the host runs it writes the
    store's exit record {the caller's rsp at its `call`, the return address, r12}, and clears
    its sp after (S0013 3a)."""
    saved = [R8, R9, R10, R11] if win64 else [RSI, RDI, R8, R9, R10, R11]
    pad = (32 if win64 else 0) + 8 * (len(saved) % 2)  # 8 + 8 * (saved + 1) + pad = 0 mod 16
    status = Mem(R15, 8 * HOST_ERROR)
    a.endbr64()
    for r in saved:
        a.push(r)
    n = 8 * len(saved)  # r8 and r9 are saved in both conventions: free until the host call
    a.mov(R8, Mem(R15, EXIT), w=True)
    a.lea(R9, Mem(RSP, n + 8))
    a.mov(Mem(R8), R9, w=True)
    a.mov(R9, Mem(RSP, n), w=True)
    a.mov(Mem(R8, 8), R9, w=True)
    a.mov(Mem(R8, 16), R12, w=True)
    a.mov(RCX, status, w=True)
    a.push(RCX)  # the enclosing host call's preset (nested entries share contexts)
    if pad:
        a.alu_imm("sub", RSP, pad, w=True)
    a.mov_imm(status, STATUS_HOST_ERROR, w=True)
    if win64:
        a.mov(RCX, R15, w=True)  # rdx already holds the slots
        a.mov(R8, RAX)  # the word (D75); r8 was saved above, free after the exit record
    else:
        a.mov(RDI, R15, w=True)
        a.mov(RSI, RDX, w=True)
        a.mov(RDX, RAX)  # the word (D75), 32-bit: zero-extended
    a.call(Mem(R15, 0, RAX, 8))
    if pad:
        a.alu_imm("add", RSP, pad, w=True)
    a.mov(RCX, Mem(R15, EXIT), w=True)  # r15 is callee-saved for the host
    a.mov_imm(Mem(RCX), 0, w=True)  # no wasm frame waits in a host call any more
    a.mov(RAX, status)  # 32-bit: zero-extended
    a.pop(RCX)
    a.mov(status, RCX, w=True)
    for r in reversed(saved):
        a.pop(r)
    a.ret()


def tail_switch(a: Asm) -> int:
    """A tail call into a function record whose context is not the caller's (D30, S0015 form
    2b). Entered by `jmp [r15 + 8 * TAIL_SWITCH]` after the caller has torn down its frame:
    rdx = the record, the arguments at r12, [rsp] = the caller's return address (rsp = 8 mod 16).
    That caller was called inside its module and expects its pool registers and r12-r15 back:
    the first hop leaves a restore frame, the pool registers a C entry may clobber and r15, then
    a real `call` whose return address is `restore`. A hop whose return address is already
    `restore` (a body this trampoline called) only switches, so a chain A -> B -> A -> ... holds
    one restore frame at most and every `ret` matches its `call` (CET shadow stacks, R2). The
    switch: a context of this compiler (its HOST_BRIDGE word is ours, D71) gets r15, r13/r14
    and a jump to the body behind its C entry, as before S0015; another compiler's, a jump to
    its C entry with rdi = the context and rsi = the frame (a C sibling call). A body entered
    through its module's C entry never comes here: it returns the hop status (backend.py).
    Returns the offset of `restore`; the status stays in eax."""
    hop, foreign, restore = a.label(), a.label(), a.label()
    a.endbr64()
    a.lea_label(RAX, restore)
    a.alu("cmp", RAX, Mem(RSP), w=True)
    a.jcc("e", hop, short=True)
    for r in POOL_VOLATILE:
        a.push(r)
    a.push(R15)  # rsp = 0 mod 16: the callee sees 8 mod 16
    a.call(hop)
    a.bind(restore)
    at = len(a.buf)
    a.pop(R15)
    for r in reversed(POOL_VOLATILE):
        a.pop(r)
    a.mov(R13, Mem(R15, MEM0), w=True)  # the callee may have grown an imported memory
    a.mov(R14, Mem(R15, MEM0 + 8), w=True)
    a.ret()
    a.bind(hop)
    a.mov(RCX, Mem(RDX, 16), w=True)
    a.mov(RAX, Mem(RCX, 8 * HOST_BRIDGE), w=True)
    a.alu("cmp", RAX, Mem(R15, 8 * HOST_BRIDGE), w=True)
    a.jcc("ne", foreign, short=True)
    a.mov(R15, RCX, w=True)
    a.mov(R13, Mem(R15, MEM0), w=True)
    a.mov(R14, Mem(R15, MEM0 + 8), w=True)
    a.mov(RCX, Mem(RDX), w=True)
    a.alu_imm("add", RCX, 16, w=True)
    a.jmp(RCX)
    a.bind(foreign)
    a.mov(RDI, RCX, w=True)
    a.mov(RSI, R12, w=True)
    a.jmp(Mem(RDX))
    return at


# The S0006 fault handler, Linux x86-64: the kernel's siginfo_t and ucontext_t (the same for
# glibc, musl and bionic); the saved `struct sigaction` is the libc's (glibc's and musl's agree:
# sa_handler first; bionic's LP64 one starts with int sa_flags, the handler at 8: S0006 step 5,
# bionic libc/include/bits/signal_types.h; its ucontext_t, libc/include/sys/ucontext.h, is the
# kernel's frame with REG_RIP 16 as below).
SI_CODE, SI_ADDR = 8, 16  # si_signo, si_errno, si_code, padding, then si_addr
UC_RIP = 40 + 8 * 16  # uc_flags, uc_link, uc_stack (24 bytes), then mcontext gregs[REG_RIP = 16]
BIONIC_SA_HANDLER = 8
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
# Windows x64 (S0006 step 4), from the SDK's winnt.h and excpt.h as mingw-w64 has them
# (mingw-w64-headers, master e3d18511, read 2026-11-03; offsets checked with clang's offsetof
# for x86_64- and aarch64-w64-windows-gnu): EXCEPTION_POINTERS {ExceptionRecord, ContextRecord};
# EXCEPTION_RECORD {DWORD ExceptionCode, DWORD ExceptionFlags, ExceptionRecord *, PVOID
# ExceptionAddress, DWORD NumberParameters, ULONG_PTR ExceptionInformation[15] at 32}: for an
# access violation [0] is read (0) / write (1) / execute (8) and [1] the address. CONTEXT:
# P1Home-P6Home, ContextFlags, MxCsr, six segment words, EFlags, Dr0-Dr3, Dr6, Dr7, Rax-R15,
# then Rip at 0xF8. STATUS_ACCESS_VIOLATION 0xC0000005 (STATUS_IN_PAGE_ERROR, a paging I/O
# failure, is never ours); EXCEPTION_CONTINUE_EXECUTION -1, EXCEPTION_CONTINUE_SEARCH 0.
WIN_EP_RECORD, WIN_EP_CONTEXT = 0, 8
WIN_ER_CODE, WIN_ER_NPARAMS, WIN_ER_INFO = 0, 24, 32
WIN_CTX_RIP = 0xF8
WIN_ACCESS_VIOLATION = 0xC0000005


def fault_handler(
    a: Asm,
    tables: int,
    prev: int,
    sigaction: int,
    reraise: int,
    darwin: bool = False,
    windows: bool = False,
    bionic: bool = False,
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
    `bionic` (Android, S0006 step 5): `prev` is bionic's struct sigaction, sa_handler at 8.
    `windows` (S0006 step 4): a vectored exception handler, `LONG h(EXCEPTION_POINTERS *ep)` in
    the Win64 convention (ep in rcx, kept in rdx): ours is an access violation (with its two
    parameters) whose ContextRecord->Rip is a fault point and whose ExceptionInformation[1] lies
    in a reservation; then Rip becomes the resume address and the handler returns
    EXCEPTION_CONTINUE_EXECUTION (-1), else EXCEPTION_CONTINUE_SEARCH (0): the OS walks the
    vectored list itself, so nothing is chained and `prev`, `sigaction` and `reraise` are
    unused. It calls nothing (no shadow space) and writes only rax, rcx, rdx and r8-r11, all
    volatile in Win64. Wasmtime's `vectored_exceptions.rs` (read, not copied) does the same.
    The idea of resuming at the module's trap stub and of chaining is poc/w2jit_guard.py's;
    "ours by PC and fault address" is Wasmtime's `test_if_trap` (read, not copied)."""
    notours, scan, skip, search, bisect, below, found, rscan, rskip, ours, dfl, done = (
        a.label() for _ in range(12)
    )
    a.endbr64()  # entered indirectly by the kernel (CET IBT)
    if windows:
        a.mov(RDX, RCX, w=True)  # ep, kept here as uc is on Unix
        a.mov(RAX, Mem(RDX, WIN_EP_RECORD), w=True)
        a.alu_imm("cmp", Mem(RAX, WIN_ER_CODE), WIN_ACCESS_VIOLATION)
        a.jcc("ne", notours)
        a.alu_imm("cmp", Mem(RAX, WIN_ER_NPARAMS), 2)
        a.jcc("b", notours)
    else:
        a.alu_imm("cmp", Mem(RSI, SI_CODE), 0)
        a.jcc("le", notours)  # kill/raise/sigqueue: never ours (on Linux)
    # where the PC is: behind a pointer (Darwin's uc_mcontext, Windows' ContextRecord) or in uc
    ptr, pc = (WIN_EP_CONTEXT, WIN_CTX_RIP) if windows else (DARWIN_UC_MCONTEXT, DARWIN_MC_RIP)
    if darwin or windows:
        a.mov(R8, Mem(RDX, ptr), w=True)
        a.mov(R8, Mem(R8, pc), w=True)
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
    if windows:
        a.mov(R9, Mem(RDX, WIN_EP_RECORD), w=True)
        a.mov(R9, Mem(R9, WIN_ER_INFO + 8), w=True)  # ExceptionInformation[1]
    else:
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
    if darwin or windows:
        a.mov(RAX, Mem(RDX, ptr), w=True)
        a.mov(Mem(RAX, pc), R8, w=True)
    else:
        a.mov(Mem(RDX, UC_RIP), R8, w=True)
    if windows:
        a.mov_imm(RAX, -1)  # EXCEPTION_CONTINUE_EXECUTION
        a.ret()
        a.bind(notours)
        a.alu("xor", RAX, RAX)  # EXCEPTION_CONTINUE_SEARCH: the next vectored handler
        a.ret()
        return
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
        a.mov(RAX, Mem(RAX, BIONIC_SA_HANDLER if bionic else 0), w=True)  # sa_handler/sigaction
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
