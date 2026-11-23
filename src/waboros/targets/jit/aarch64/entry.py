"""The runtime's aarch64 `entry` and stack-pointer stub (docs/specs/S0015, "The runtime's `entry`"):
the host's side of the boundary, not a compiler's. `entry` follows the contract and the reference
listing of S0015's appendix: AAPCS64 on Linux, macOS and Windows alike, so the host's callee-saved
registers are the boundary's and it saves only x19 (with x20, a pair), which keeps ctx across the
call. It pushes a frame record, keeps the store's enclosing exit record and the frame in the saved
block at [sp + 96, sp + 128) of its `blr`, calls a C entry `int32_t f(x0 = ctx, x1 = frame)`, puts
the record back and returns the status as 32 bits (`mov w0, w0`), kept zero-extended in
ctx[TRAP_CODE] when non-zero. It sets up no pinned register (each compiler's C entry does) and never
touches x18. Both start with `bti jc`: anything at the boundary is reached by `blr` or by `br`
through any register (S0015 Q5).

The artifact does not carry these (S0015): `aarch64.runtime()` builds them for the process.
"""

from waboros.targets.jit.aarch64.encoder import FP, LR, SP, Asm
from waboros.targets.jit.common.context import EXIT_RECORD, TRAP_CODE

EXIT = 8 * EXIT_RECORD
ENTRY_SAVED = 96  # the saved block {sp, pc, frame of the exit record, frame} from sp at the blr


def entry(a: Asm) -> int:
    """int32_t entry(x0 = ctx, x1 = frame, x2 = c_entry); returns the offset of its return point
    (the walk's entry point)."""
    done = a.label()
    a.bti("jc")
    a.pair(False, FP, LR, SP, -128, "pre")
    a.mov(FP, SP)
    a.pair(False, 19, 20, SP, 16)  # x19 keeps ctx across the call
    a.mov(19, 0)
    a.ld(9, 19, EXIT)
    a.pair(True, 10, 11, 9)
    a.ld(12, 9, 16)
    a.pair(False, 10, 11, SP, ENTRY_SAVED)
    a.pair(False, 12, 1, SP, ENTRY_SAVED + 16)
    a.blr(2)
    ret = len(a.buf)
    a.mov(0, 0, wide=False)  # the status is 32 bits: the upper half of x0 is undefined
    a.ld(9, 19, EXIT)
    a.pair(True, 10, 11, SP, ENTRY_SAVED)
    a.ld(12, SP, ENTRY_SAVED + 16)
    a.pair(False, 10, 11, 9)
    a.st(12, 9, 16)
    a.cbz(0, done, wide=False)
    a.st(0, 19, 8 * TRAP_CODE)
    a.bind(done)
    a.pair(True, 19, 20, SP, 16)
    a.pair(True, FP, LR, SP, 128, "post")
    a.ret()
    return ret


def sp_stub(a: Asm) -> None:
    """A C entry: frame[0] = the stack pointer as a C entry sees it on entry."""
    a.bti("jc")
    a.mov(16, SP)
    a.st(16, 1)
    a.mov_imm(0, 0, wide=False)
    a.ret()
