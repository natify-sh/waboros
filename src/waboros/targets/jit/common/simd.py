# pyright: reportPrivateUsage=false
"""Fixed-width SIMD (v128) in the one-pass JIT core (docs/specs/S0005), shared by every
architecture: the stack effects of the v128 operators, `select`/`drop`/locals/globals of v128,
and the boundary split/join (pure arithmetic on ints; `instance.py` calls it, never the reverse).

A v128 value takes two slots k, k+1 (bytes 0-7 in k, ARTIFACT.md part 3). On the core's lazy
stack it is two entries, both constants (the two halves) or both in their slots: flush, branch
moves, calls and block parameters move halves like any 64-bit value. Only the backend's `simd`
method sees a pair, always as `Slot(k)`: a constant pair is written into its own slots first. A
v128 local is copied into the stack's slots when read (never a lazy local entry)."""

import re
from collections.abc import Sequence
from typing import Any

from waboros.frontend import simd as fs
from waboros.targets.jit.common import compiler as core
from waboros.targets.jit.common.backend import Imm, Operand, Slot

V128 = 0x7B
M64 = (1 << 64) - 1
SCRATCH = 10  # slots from the base of a SIMD operator's operands: 6 for them, 4 scratch (S0005)


# ---------------------------------------------------------------------- for the backends
_LANE = {"i8x16": 1, "i16x8": 2, "i32x4": 4, "i64x2": 8, "f32x4": 4, "f64x2": 8}


def shape(op: int) -> tuple[str, int, bool]:
    """(kind, bytes per lane or access, signed) of a SIMD operator, from its name: kind is
    `splat`, `extract`, `replace`, `load_splat`, `load_zero`, `load_lane`, `store_lane`, or the
    rest of the name (`load`, `and`, `any_true`, ...; 16 bytes)."""
    pre, _, rest = fs.OPS[op - fs.BASE].name.partition(".")
    if pre != "v128":  # i8x16.splat, i16x8.extract_lane_s, f64x2.replace_lane, i32x4.add ...
        return rest.split("_lane")[0], _LANE[pre], rest.endswith("_s")
    m = re.fullmatch(r"(load|store)(8|16|32|64)_(splat|zero|lane)", rest)
    if m:  # v128.load8_splat, v128.load32_zero, v128.store16_lane ...
        return f"{m[1]}_{m[3]}", int(m[2]) // 8, False
    return rest, 16, False


_MOVES = "load store const not and andnot or xor bitselect any_true splat extract replace"
# the data-movement operators (S0005 step 5c): what both backends compile so far
MOVES = frozenset(
    fs.BASE + sub
    for sub in fs.OPS
    if shape(fs.BASE + sub)[0] in (*_MOVES.split(), "load_splat", "load_zero", "load_lane",
                                   "store_lane")
)  # fmt: skip


def width(ts: Sequence[Any]) -> int:
    """Slots taken by values of the types `ts` (v128 = 2, everything else 1)."""
    return len(ts) + sum(t == V128 for t in ts)


def split(ts: Sequence[Any], vals: Sequence[Any]) -> list[Any]:
    """Values of the types `ts` as slot words: a v128 (an int in [0, 2^128)) as (low, high)."""
    out: list[Any] = []
    for t, v in zip(ts, vals, strict=True):
        out += (v & M64, v >> 64 & M64) if t == V128 else (v,)
    return out


def join(ts: Sequence[Any], slots: Sequence[Any]) -> list[Any]:
    """The inverse of `split`."""
    out: list[Any] = []
    k = 0
    for t in ts:
        if t == V128:
            out.append(slots[k] | slots[k + 1] << 64)
            k += 2
        else:
            out.append(slots[k])
            k += 1
    return out


# ---------------------------------------------------------------------- the core's side
def _top(fn: "core._Func") -> int:
    """The slot a new entry gets (a pending compare becomes a value first)."""
    if fn.st and fn.st[-1][0] == core.CMP:
        fn.uncmp()
    return fn.L + len(fn.st)


def _push2(fn: "core._Func") -> None:
    fn.push(core.SLOTE)
    fn.push(core.SLOTE)


def _halves(fn: "core._Func", i: int) -> tuple[Operand, Operand]:
    """The two halves of the v128 whose low entry is stack entry i (not popped)."""
    lo, hi = fn.st[i], fn.st[i + 1]
    if lo[0] == core.CONST:
        assert hi[0] == core.CONST
        return Imm(lo[1]), Imm(hi[1])
    assert lo == hi == core.SLOTE, (lo, hi)
    return Slot(fn.L + i), Slot(fn.L + i + 1)


def _pair(fn: "core._Func") -> Slot:
    """Pop a v128 as its slots (a constant written into them first)."""
    i = len(fn.st) - 2
    lo, hi = _halves(fn, i)
    if isinstance(lo, Imm):
        fn.b.move(Slot(fn.L + i), lo, True)
        fn.b.move(Slot(fn.L + i + 1), hi, True)
    del fn.st[i:]
    return Slot(fn.L + i)


def const(fn: "core._Func", v: int) -> None:
    fn.push((core.CONST, v & M64))
    fn.push((core.CONST, v >> 64))


def get_local(fn: "core._Func", j: int) -> None:
    k, s = _top(fn), fn.slot[j]
    fn.b.move(Slot(k), Slot(s), True)
    fn.b.move(Slot(k + 1), Slot(s + 1), True)
    _push2(fn)


def set_local(fn: "core._Func", j: int, tee: bool) -> None:
    """No stack entry refers to a v128 local (reads copy it), so nothing keeps the old value."""
    i, s = len(fn.st) - 2, fn.slot[j]
    lo, hi = _halves(fn, i)
    fn.b.move(Slot(s), lo, True)
    fn.b.move(Slot(s + 1), hi, True)
    if not tee:
        del fn.st[i:]


def drop(fn: "core._Func") -> None:
    del fn.st[-2:]


def select(fn: "core._Func") -> None:
    """t if the condition holds, else e: e's halves over t's slots behind a branch on it."""
    b = fn.b
    cond, x, y, w = fn.pop_cond()
    i = len(fn.st) - 4
    k = fn.L + i
    t, e = _halves(fn, i), _halves(fn, i + 2)
    if isinstance(t[0], Imm):
        b.move(Slot(k), t[0], True)
        b.move(Slot(k + 1), t[1], True)
    keep = b.new_label()
    b.cmp_branch(cond, x, y, keep, w)
    b.move(Slot(k), e[0], True)
    b.move(Slot(k + 1), e[1], True)
    b.bind(keep)
    del fn.st[i:]
    _push2(fn)


def global_get(fn: "core._Func", op: int, word: int, indirect: bool) -> None:
    if op == 0x23:
        fn.b.simd(fn.f, op, Slot(_top(fn)), (), (word, indirect))
        _push2(fn)
    else:
        fn.b.simd(fn.f, op, None, (_pair(fn),), (word, indirect))


def op(fn: "core._Func", ins: Sequence[Any]) -> None:
    """A `0xFD` operator: operands popped in wasm order (v128 as `Slot`, scalars as the core's
    operands), the result in the operands' first slots (v128) or `tos` (a scalar)."""
    o = fs.OPS[ins[0] - fs.BASE]
    if o.imm == fs.CONST:
        const(fn, ins[1])
        return
    args: list[Operand] = [_pair(fn) if t == V128 else fn.pop() for t in reversed(o.pops)]
    args.reverse()
    h = len(fn.st)
    imm: tuple[int, ...] = ()
    if o.imm in (fs.MEM, fs.MEMLANE):
        imm = (ins[1], fn.m.layout.memory(ins[3]), *ins[4:])
    elif o.imm == fs.LANE:
        imm = (ins[1],)
    elif o.imm == fs.SHUFFLE:
        imm = (int.from_bytes(ins[1], "little"),)
    dst = None
    if o.res == V128:
        dst = Slot(fn.L + h)
    elif o.res:
        fn.need_tos()
        dst = fn.TOS
    fn.b.simd(fn.f, ins[0], dst, tuple(args), imm)
    fn.maxh = max(fn.maxh, h + SCRATCH)
    if o.res == V128:
        _push2(fn)
    elif o.res:
        fn.push((core.TOS, None))
