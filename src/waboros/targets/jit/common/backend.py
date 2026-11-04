"""The backend call interface of the one-pass JIT core (D26: LIR as a call interface, S0002).

Three-operand, no flags: a compare is consumed inside the same call (`cmp_branch`, `cmp_set`,
`select_cmp`), so no code can land between a flags producer and its consumer (LESSONS 1). The
backend owns its register roles: `pool` (pinnable, kept across every call) and `tos` (the result
register); the core never names a machine register. Between two calls only `tos` and pool
registers hold values; everything else is the backend's scratch.

Operands: `Imm` (the value of the operand's width, unsigned), `Reg` (a backend register id), `Slot`
(value-stack slot k of the current frame). `wide`: a 64-bit operation, else 32-bit with the result
zero-extended (LESSONS 5). Slots always hold zero-extended 64-bit values. `AddK` (S0007 K): the
i32 `(id + k) mod 2**32`, accepted only by `move` (as `src`, wide=False) and as the `load`/`store`
address, each formed by one 32-bit add into the destination or the index scratch (no flags, no
other register written).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar


@dataclass(frozen=True, slots=True)
class Imm:
    value: int


@dataclass(frozen=True, slots=True)
class Reg:
    id: int


@dataclass(frozen=True, slots=True)
class Slot:
    k: int


@dataclass(frozen=True, slots=True)
class AddK:  # the i32 value (register `id` + k) mod 2**32 (S0007 K)
    id: int  # a pool register holding a zero-extended i32 (a pinned local)
    k: int  # 0 <= k < 2**32


Operand = Imm | Reg | Slot
Dst = Reg | Slot

# binop: add sub mul and or xor shl shr_s shr_u rotl rotr; divrem: div_s div_u rem_s rem_u;
# unop: clz ctz popcnt extend8_s extend16_s extend32_s extend_i32_s extend_i32_u wrap
# cond: eq ne lt_s lt_u gt_s gt_u le_s le_u ge_s ge_u
SWAP = {"eq": "eq", "ne": "ne", "lt_s": "gt_s", "lt_u": "gt_u", "gt_s": "lt_s", "gt_u": "lt_u",
        "le_s": "ge_s", "le_u": "ge_u", "ge_s": "le_s", "ge_u": "le_u"}  # fmt: skip
NEGATE = {"eq": "ne", "ne": "eq", "lt_s": "ge_s", "lt_u": "ge_u", "gt_s": "le_s", "gt_u": "le_u",
          "le_s": "gt_s", "le_u": "gt_u", "ge_s": "lt_s", "ge_u": "lt_u"}  # fmt: skip

L = TypeVar("L")  # the backend's label type
F = TypeVar("F")  # the backend's per-function frame


class Backend(Protocol[L, F]):
    pool: tuple[int, ...]
    tos: int
    supported: frozenset[int]  # the opcodes this backend compiles (S0004)

    # frames, traps, labels. A function whose conditional branches do not all reach their
    # targets (func_end returns False) is dropped (`rewind` to its start) and compiled again
    # with far=True (S0004, far branches); a backend whose branches always reach never asks.
    def new_label(self) -> L: ...
    def bind(self, label: L) -> None: ...
    def jump(self, label: L) -> None: ...
    def here(self) -> int: ...  # code offset of the next instruction
    def func_begin(self, label: L, pins: tuple[int, ...] = (), far: bool = False) -> F: ...
    def func_end(self, f: F, frame_slots: int) -> bool: ...
    def rewind(self, pos: int) -> None: ...
    def poll(self, f: F) -> None: ...  # interrupt check (loop headers)
    def trap(self, f: F, code: int) -> None: ...
    def ret_ok(self, f: F) -> None: ...
    def finish(self) -> tuple[bytes, int]: ...  # (code + data area, offset of the data area)

    # data
    def move(self, dst: Dst, src: Operand | AddK, wide: bool) -> None: ...
    def binop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None: ...
    def divrem(self, f: F, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None: ...
    def unop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None: ...
    def cmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None: ...
    def cmp_branch(self, cond: str, x: Operand, y: Operand, label: L, wide: bool) -> None: ...
    def select_cmp(
        self, cond: str, x: Operand, y: Operand, wc: bool, dst: Reg, t: Operand, e: Operand,
        wv: bool,
    ) -> None: ...  # dst = t if (x cond y) else e  # fmt: skip
    def jump_table(self, index: Operand, labels: Sequence[L], default: L) -> None: ...

    # floats: operands and results are bit patterns (f32 zero-extended), as everywhere else.
    # fbinop: add sub mul div min max copysign; funop: abs neg ceil floor trunc nearest sqrt;
    # fcmp_set: eq ne lt gt le ge (dst = 0/1); convert: a wasm conversion by its name
    # ("i32.trunc_f64_s", "i64.trunc_sat_f32_u", "f32.convert_i64_u", "f64.promote_f32", ...)
    def fbinop(self, op: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None: ...
    def funop(self, op: str, dst: Reg, x: Operand, wide: bool) -> None: ...
    def fcmp_set(self, cond: str, dst: Reg, x: Operand, y: Operand, wide: bool) -> None: ...
    def convert(self, f: F, op: str, dst: Reg, x: Operand) -> None: ...

    # memories (`mem`: the memory's `base` word, `size_bytes` follows; explicit bounds checks)
    # and context words (globals)
    def memory_size(self, dst: Reg, mem: int) -> None: ...  # in pages
    def load(
        self, f: F, size: int, signed: bool, wide: bool, dst: Reg, addr: Operand | AddK,
        offset: int, mem: int,
    ) -> None: ...  # fmt: skip
    def store(
        self, f: F, size: int, addr: Operand | AddK, offset: int, src: Operand, mem: int
    ) -> None: ...  # fmt: skip
    def ctx_load(self, dst: Reg, word: int, indirect: bool) -> None: ...
    def ctx_store(self, word: int, src: Operand, indirect: bool) -> None: ...

    def ctx_addr(self, dst: Reg, word: int) -> None: ...  # a function record (ref.func)

    # SIMD (S0005, the D39 amendment): `op` = 0xFD000 + sub-opcode, or 0x23/0x24 for a v128
    # global (`imm` = (word, indirect)); `args` in wasm order. A v128 operand or result is
    # `Slot(k)` (slots k, k+1, low half first); scalars as everywhere else; `dst` None for an
    # operator without a result. Reads every operand before writing `dst`; checks bounds as `load`
    # and `store`; preserves pool, frame, context, memory-0 registers and `tos` (unless `dst`);
    # leaves no flags and no vector register live; may use the 4 slots from 6 above the base of
    # its operands. `imm`: lane, `(offset, memory word[, lane])`, the shuffle lanes as one int.
    def simd(
        self, f: F, op: int, dst: Dst | None, args: tuple[Operand, ...], imm: tuple[int, ...]
    ) -> None: ...  # fmt: skip
    # a v128 slot pair (`Slot`) or a 128-bit constant (`Imm`) into the pair `dst` by one 16-byte
    # store, the width the next reader uses (store forwarding, LESSONS 44); no flags, no pool
    # register or `tos` changed, no vector register left live
    def move_v128(self, dst: Slot, src: Slot | Imm) -> None: ...

    # wide arithmetic (S0008, a D39 addition only 0xFC13-0xFC16 reach): `op` add128 | sub128
    # (args a_lo, a_hi, b_lo, b_hi) | mul_wide_s | mul_wide_u (args x, y), wasm order, i64 operands;
    # the low 64 bits into `dst`, the high into `Slot(dst.k + 1)`. `dst.k` is the first operand's
    # stack slot, so an operand slot may be overwritten, each only after it is read. May write
    # `tos` (the core spills it first); preserves pool, frame, context and memory-0 registers;
    # leaves no flags live; cannot trap.
    def wide(self, op: str, dst: Slot, args: tuple[Operand, ...]) -> None: ...

    # tables (`word`: the table's `base` word; `size` follows) and bulk memory: bounds checked
    # before anything is written; operands are i32 but table values (64-bit references)
    def table_get(self, f: F, dst: Reg, word: int, index: Operand) -> None: ...
    def table_set(self, f: F, word: int, index: Operand, value: Operand) -> None: ...
    def table_fill(self, f: F, word: int, i: Operand, v: Operand, n: Operand) -> None: ...
    def table_copy(
        self, f: F, dst: int, src: int, d: Operand, s: Operand, n: Operand
    ) -> None: ...  # fmt: skip
    def memory_fill(self, f: F, d: Operand, v: Operand, n: Operand, mem: int) -> None: ...
    def memory_copy(
        self, f: F, d: Operand, s: Operand, n: Operand, dmem: int, smem: int
    ) -> None: ...  # fmt: skip

    # calls (a non-zero status unwinds the caller too)
    def call_direct(self, f: F, label: L, shift_slots: int) -> None: ...
    def call_host(self, f: F, word: int, slot: int) -> None: ...
    def call_indirect(
        self, f: F, table: int, sig: int, index: Operand, shift_slots: int
    ) -> None: ...  # fmt: skip
    def call_ref(self, f: F, ref: Operand, shift_slots: int) -> None: ...  # null: NULL_REFERENCE
    def call_record(self, f: F, word: int, shift_slots: int) -> None: ...  # a function import
    def host_thunk(self, word: int) -> int: ...  # code offset of an import's record entry
    def null_check(self, f: F, x: Operand, code: int) -> None: ...  # trap if the reference is 0

    # tail calls: the arguments (slots src..src+n) become the callee's frame at our frame base,
    # our frame is dropped, the callee returns to our caller (constant stacks)
    def tail_direct(self, f: F, label: L, src: int, n: int) -> None: ...
    def tail_indirect(
        self, f: F, table: int, sig: int, index: Operand, src: int, n: int
    ) -> None: ...  # fmt: skip
    def tail_ref(self, f: F, ref: Operand, src: int, n: int) -> None: ...
    def tail_record(self, f: F, word: int, src: int, n: int) -> None: ...
