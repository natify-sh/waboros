"""The runtime's x86-64 `entry` and stack-pointer stub (docs/specs/S0015, "The runtime's `entry`"):
the host's side of the boundary, not a compiler's. `entry` follows the contract and the reference
listings of S0015's appendix (SysV and Win64 hosts): it saves what the host needs kept that the
boundary convention does not keep, keeps the store's enclosing exit record and the frame in the
saved block at [rsp, rsp + 32) of its call, calls a C entry `int32_t f(ctx, frame)` (System V on
every OS), puts the record back and returns the status as 32 bits, kept in ctx[TRAP_CODE] when
non-zero. It sets up no pinned register: each compiler's C entry does that.

The artifact does not carry these (S0015): `x86_64.runtime()` builds them for the process.
"""

from waboros.targets.jit.common.context import EXIT_RECORD, TRAP_CODE
from waboros.targets.jit.x86_64 import simd
from waboros.targets.jit.x86_64.encoder import R8, RAX, RBX, RCX, RDI, RDX, RSI, RSP, Asm, Mem

EXIT = 8 * EXIT_RECORD
ENTRY_SAVED = 0  # the saved block {sp, pc, frame of the exit record, frame} from rsp at the call
XMM_SAVED = range(6, 16)  # Win64 callee-saved, SysV caller-saved: [rsp + 32 + 16k]


def entry(a: Asm, win64: bool) -> int:
    """int32_t entry(uint64_t *ctx, uint64_t *frame, const void *c_entry) in the host's
    convention; returns the offset of its return point (the walk's entry point)."""
    a.endbr64()
    a.push(RBX)  # rbx keeps ctx across the call (boundary callee-saved)
    if win64:
        a.push(RSI)
        a.push(RDI)  # rsp = 0 mod 16
        a.mov(RDI, RCX, w=True)  # the SysV arguments: ctx
        a.mov(RSI, RDX, w=True)  # frame
        a.alu_imm("sub", RSP, 32 + 16 * len(XMM_SAVED), w=True)  # no shadow space: SysV callee
        for k, x in enumerate(XMM_SAVED):
            simd.movups(a, x, Mem(RSP, 32 + 16 * k), True)
    else:
        a.alu_imm("sub", RSP, 32, w=True)  # rsp = 0 mod 16
    a.mov(RBX, RDI, w=True)
    a.mov(RAX, Mem(RBX, EXIT), w=True)
    for i in range(3):
        a.mov(RCX, Mem(RAX, 8 * i), w=True)
        a.mov(Mem(RSP, ENTRY_SAVED + 8 * i), RCX, w=True)
    a.mov(Mem(RSP, ENTRY_SAVED + 24), RSI, w=True)
    a.call(R8 if win64 else RDX)
    ret = len(a.buf)
    a.mov(RCX, Mem(RBX, EXIT), w=True)
    for i in range(3):
        a.mov(RDX, Mem(RSP, ENTRY_SAVED + 8 * i), w=True)
        a.mov(Mem(RCX, 8 * i), RDX, w=True)
    a.mov(RAX, RAX)  # the status is 32 bits: the upper half of rax is undefined
    a.test(RAX, RAX)
    done = a.label()
    a.jcc("e", done, short=True)
    a.mov(Mem(RBX, 8 * TRAP_CODE), RAX, w=True)
    a.bind(done)
    if win64:
        for k, x in enumerate(XMM_SAVED):
            simd.movups(a, x, Mem(RSP, 32 + 16 * k), False)
        a.alu_imm("add", RSP, 32 + 16 * len(XMM_SAVED), w=True)
        a.pop(RDI)
        a.pop(RSI)
    else:
        a.alu_imm("add", RSP, 32, w=True)
    a.pop(RBX)
    a.ret()
    return ret


def sp_stub(a: Asm) -> None:
    """A C entry: frame[0] = the stack pointer as a C entry sees it on entry."""
    a.endbr64()
    a.mov(Mem(RSI), RSP, w=True)
    a.alu("xor", RAX, RAX)
    a.ret()
