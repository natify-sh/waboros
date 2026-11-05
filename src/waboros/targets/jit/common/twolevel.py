"""The bounds checks of memory accesses (S0011 step 1), the end checks of bulk memory and tables
and the table element check (step 2), written once over each backend's low layer.

Test-only switch until S0011 step 4: `WABOROS_JIT_TWOLEVEL=1` makes `_bounds`, `_end_check` and
`_element` of both backends delegate here; off, they emit their own code. The code is the same
either way except the listed modules (S0011, "Identity"). The low layer (`LowLayer`, S0011 "The
low-layer contract") are the `ll_*` methods of each backend: they name the machine registers and
fit every immediate to its encoding; this module names neither. Conditions are D39's names,
unsigned 64-bit."""

from typing import Protocol, TypeVar

from waboros.targets.jit.common.backend import AddK, Imm, Operand
from waboros.targets.jit.common.guard import GUARD

M32, M64 = 0xFFFFFFFF, (1 << 64) - 1

L = TypeVar("L", contravariant=True)  # the backend's label
H = TypeVar("H")  # a memory's base or limit, as the backend holds it (a register, a context word)
A = TypeVar("A", covariant=True)  # the access the backend's load/store/simd emit
E = TypeVar("E", covariant=True)  # a table element as the backend's table ops read it


class LowLayer(Protocol[L, H, A, E]):
    IDX: int  # the index scratch register
    TMP: int  # the check's scratch register
    guard: bool  # S0006: guard-mode code

    def ll_mem(self, mem: int) -> tuple[H, H]: ...  # (base, size in bytes) of context word `mem`
    def ll_fault_next(self, label: L) -> None: ...  # the next memory instruction faults to label
    def ll_jump(self, label: L) -> None: ...
    def ll_index32(self, addr: Operand | AddK) -> int: ...  # IDX = addr mod 2**32
    def ll_index64(self, addr: Operand) -> int: ...  # a register holding the 64-bit index
    def ll_fold(self, r: int, offset: int, size: int) -> tuple[int, int]: ...  # r + offset
    def ll_add(self, dst: int, src: int | H, k: int) -> int: ...  # dst = src + k mod 2**64
    def ll_add_jc(self, dst: int, src: int, k: int, label: L) -> int: ...  # carry: to label
    def ll_jcmp(self, cond: str, x: int, y: int | H, label: L) -> None: ...
    def ll_jcmp_imm(self, cond: str, x: int | H, v: int, label: L) -> None: ...
    def ll_access(self, base: H, r: int | None, disp: int, size: int) -> A: ...
    def ll_check_sum(self, r: int, n: int, limit: H, label: L, wide: bool) -> None: ...
    def ll_index(self, addr: Operand, wide: bool) -> int: ...  # IDX = addr, 64-bit or zero-ext.
    def ll_ctx_word(self, word: int) -> H: ...  # context word `word` as a handle
    def ll_element(self, word: int, r: int) -> E: ...  # 8 bytes at [context word `word`] + 8 r


def bounds(
    b: LowLayer[L, H, A, E], oob: L, addr: Operand | AddK, offset: int, size: int, mem: int,
    a64: bool, lo: int,
) -> A:  # fmt: skip
    """Check `addr + offset .. + size` against the memory's size (R3: the one place memory safety
    rests on) and return the access; `oob` is the function's MEMORY_OUT_OF_BOUNDS stub. An i32
    index is zero-extended (the wasm wrap comes before the offset) and the offset (< 2**32) added
    in 64 bits, so nothing wraps. Guard mode (S0006): an i32 access inside the reservation of
    4 GiB + GUARD goes unchecked and its instruction is a fault point. An i64 memory: `_i64`."""
    base, limit = b.ll_mem(mem)
    if a64 and not isinstance(addr, Imm):
        assert not isinstance(addr, AddK), "an AddK is an i32: never an i64 address"
        return _i64(b, oob, base, limit, addr, offset, size, lo)
    unchecked = (b.guard and not a64) and (
        (addr.value & M32) + offset + size <= (1 << 32) + GUARD
        if isinstance(addr, Imm)
        else offset + size <= GUARD
    )
    if unchecked:
        b.ll_fault_next(oob)
    if isinstance(addr, Imm):  # i64 (S0009): without the mask
        ea = (addr.value & (M64 if a64 else M32)) + offset
        if ea + size > M64:  # ends past 2**64: no memory holds it (LESSONS 75)
            b.ll_jump(oob)
            ea = 0
        if not unchecked:
            b.ll_jcmp_imm("lt_u", limit, ea + size, oob)
        return b.ll_access(base, None, ea, size)
    r, disp = b.ll_fold(b.ll_index32(addr), offset, size)
    if not unchecked:
        b.ll_jcmp("gt_u", b.ll_add(b.TMP, r, disp + size), limit, oob)
    return b.ll_access(base, r, disp, size)


def _i64(
    b: LowLayer[L, H, A, E], oob: L, base: H, limit: H, addr: Operand, offset: int, size: int,
    lo: int,
) -> A:  # fmt: skip
    """An i64 memory (S0009), explicit in every mode: no guard covers a 64-bit index. With
    k = offset + size <= lo (the declared minimum, so the size L >= k and L - k cannot wrap)
    idx > L - k traps; otherwise idx + k carrying out of 64 bits traps, then idx + k > L."""
    r = b.ll_index64(addr)
    if offset + size > M64:  # no index is in bounds (dead code follows, well-formed)
        b.ll_jump(oob)
        offset = 0
    k = offset + size
    if k <= lo:
        b.ll_jcmp("gt_u", r, b.ll_add(b.TMP, limit, -k), oob)
    else:
        b.ll_jcmp("gt_u", b.ll_add_jc(b.TMP, r, k, oob), limit, oob)
    r, disp = b.ll_fold(r, offset, size)
    return b.ll_access(base, r, disp, size)


def end_check(b: LowLayer[L, H, A, E], oob: L, r: int, n: int, limit: H, wide: bool) -> None:
    """Bulk memory and tables: trap to `oob` unless r + n <= limit (`n` the count register);
    `r` is preserved. Not `wide`: the caller zero-extended both (each below 2**32), so the sum
    cannot wrap. `wide`: 64-bit values, a carry out of 64 bits is out of bounds (S0009). The
    width of each operand (`n` is wide only between two wide ranges) and the order (both ranges
    of a copy before any write) stay in the callers until step 4. One fused op (S0011 Q2):
    x86-64 has no free register in memory_copy."""
    b.ll_check_sum(r, n, limit, oob, wide)


def element(b: LowLayer[L, H, A, E], oob: L, word: int, index: Operand, a64: bool) -> E:
    """A table's element check: IDX = the index (64-bit for a table64, else zero-extended,
    LESSONS 5), copied even from a register (the caller may then write the operand's register,
    LESSONS 32); index >= the size (context word `word + 1`) traps to `oob`. Returns the
    element: 8 bytes at the elements' base (context word `word`) + 8 IDX."""
    r = b.ll_index(index, a64)
    b.ll_jcmp("ge_u", r, b.ll_ctx_word(word + 1), oob)
    return b.ll_element(word, r)
