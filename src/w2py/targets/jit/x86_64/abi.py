"""x86-64 trampolines (docs/specs/S0003): the entry and the host bridge for each host calling
convention (SysV; Win64 per D23), and the stack-pointer stub. All are reached through a pointer, so
each starts with `endbr64` (CET IBT, R2); every `ret` matches its `call` (CET shadow stacks).

Register roles: r15 context, r12 value-stack frame, r13/r14 memory 0 base/size, eax status.
Alignment (LESSONS 3): rsp is 8 mod 16 on entry to any of these; each pads to 0 mod 16 before its
`call`, so wasm code always starts with rsp = 8 mod 16, as a C function does.
"""

import sys
from functools import cache

from w2py.targets.jit.common.context import HOST_ERROR, STATUS_HOST_ERROR, TRAP_CODE, Layout
from w2py.targets.jit.common.execmem import load
from w2py.targets.jit.common.instance import Trampolines
from w2py.targets.jit.x86_64.encoder import (
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


@cache
def trampolines() -> Trampolines:
    """The trampolines for this process's convention, in one sealed code area (kept for the
    process's lifetime). Both conventions are assembled everywhere (the encoder tests see both)."""
    win64 = sys.platform == "win32"
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
    mem = load(a.finish())
    return Trampolines(*(mem.addr + o for o in offsets), keep=mem)
