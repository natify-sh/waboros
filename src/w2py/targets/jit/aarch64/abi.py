"""AArch64 trampolines (docs/specs/S0004, "Trampolines"): one entry, one bridge, one sp stub, one
`tail_switch` for every OS — AAPCS64 (Linux), Apple arm64 and Windows Arm64 agree on everything
they touch (arguments in x0-x7, result in w0, callee-saved x19-x29, sp, d8-d15); x18 (the platform
register) is never used. All are reached by `blr`/`br x16|x17`, so each starts with `bti c` (R2).

Register roles: x28 context, x27 value-stack frame, x26/x25 memory 0 base/size (one `ldp` from
the context), w0 status. Every trampoline keeps sp 16-byte aligned at all times and pushes a frame
record {x29, x30} (tail_switch: a {x28, x30} restore frame); every `ret` returns to its `bl`/`blr`.
"""

from w2py.targets.jit.aarch64.encoder import FP, LR, SP, Asm
from w2py.targets.jit.common.context import (
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
