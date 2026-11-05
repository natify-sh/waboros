"""Wasm memory and table semantics written once over each backend's low layer (S0011): the
bounds checks of memory accesses, the end checks and operand rules of bulk memory and table
operators, and the table element check. The low layer (`LowLayer`, S0011 "The low-layer
contract") are the `ll_*` methods of each backend: they name the machine registers and fit
every immediate to its encoding; this module names neither. Conditions are D39's names,
unsigned 64-bit. Each backend's plug point (`_bounds`, `_element`, the bulk and table methods)
creates the trap stub and passes its label: this module never sees a frame."""

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
    def ll_three(
        self, x: Operand, y: Operand, z: Operand, wx: bool, wy: bool, wz: bool
    ) -> tuple[int, int, int]: ...  # three operands at their widths, each read before any write
    def ll_addr(self, r: int, base: H) -> None: ...  # r = base + r
    def ll_elem_addr(self, r: int, word: int) -> None: ...  # r = [context word `word`] + 8 r


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


# Bulk memory and tables (S0011 step 4). Every operand is read, at its width, before anything is
# written (`ll_three`, LESSONS 32), and every range is checked before the backend's loop writes
# (no partial fill or copy). The end check is one fused op (Q2: x86-64 has no free register in
# memory_copy): trap unless r + n <= limit, `r` preserved. Not wide, both were read zero-extended
# (each below 2**32), so the sum cannot wrap; wide, a carry out of 64 bits is out of bounds
# (S0009). The operands stay in `ll_three`'s registers for the backend's loop.
def memory_copy(
    b: LowLayer[L, H, A, E], oob: L, d: Operand, s: Operand, n: Operand, dmem: int, smem: int,
    a64: tuple[bool, bool],
) -> None:  # fmt: skip
    """Each range at its memory's width, `n` 64-bit only between two i64 memories (S0009). Each
    range is checked, then its address formed: a memory other than 0 may hold its base and
    size in the registers the other one needs (aarch64 x11/x12)."""
    ad, as_ = a64
    rd, rs, rn = b.ll_three(d, s, n, ad, as_, ad and as_)
    for r, mem, w in ((rd, dmem, ad), (rs, smem, as_)):
        base, limit = b.ll_mem(mem)
        b.ll_check_sum(r, rn, limit, oob, w)
        b.ll_addr(r, base)


def memory_fill(
    b: LowLayer[L, H, A, E], oob: L, d: Operand, v: Operand, n: Operand, mem: int, a64: bool
) -> None:  # fmt: skip
    """The range at the memory's width; the value an i32."""
    rd, _, rn = b.ll_three(d, v, n, a64, False, a64)
    base, limit = b.ll_mem(mem)
    b.ll_check_sum(rd, rn, limit, oob, a64)
    b.ll_addr(rd, base)


def table_fill(
    b: LowLayer[L, H, A, E], oob: L, word: int, i: Operand, v: Operand, n: Operand, a64: bool
) -> None:  # fmt: skip
    """The table's base and size are context words `word`, `word + 1`. The range at the
    table's width; the value a 64-bit reference."""
    ri, _, rn = b.ll_three(i, v, n, a64, True, a64)
    b.ll_check_sum(ri, rn, b.ll_ctx_word(word + 1), oob, a64)
    b.ll_elem_addr(ri, word)


def table_copy(
    b: LowLayer[L, H, A, E], oob: L, dst: int, src: int, d: Operand, s: Operand, n: Operand,
    a64: tuple[bool, bool],
) -> None:  # fmt: skip
    """Each range at its table's width, `n` the narrower (as memory_copy). Both ranges are
    checked, then both addresses formed (the order the code had before step 4 on both
    architectures)."""
    ad, as_ = a64
    rd, rs, rn = b.ll_three(d, s, n, ad, as_, ad and as_)
    for r, word, w in ((rd, dst, ad), (rs, src, as_)):
        b.ll_check_sum(r, rn, b.ll_ctx_word(word + 1), oob, w)
    for r, word in ((rd, dst), (rs, src)):
        b.ll_elem_addr(r, word)


def element(b: LowLayer[L, H, A, E], oob: L, word: int, index: Operand, a64: bool) -> E:
    """A table's element check: IDX = the index (64-bit for a table64, else zero-extended,
    LESSONS 5), copied even from a register (the caller may then write the operand's register,
    LESSONS 32); index >= the size (context word `word + 1`) traps to `oob`. Returns the
    element: 8 bytes at the elements' base (context word `word`) + 8 IDX."""
    r = b.ll_index(index, a64)
    b.ll_jcmp("ge_u", r, b.ll_ctx_word(word + 1), oob)
    return b.ll_element(word, r)
