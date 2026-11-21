"""Liveness of reference locals at the safepoints of one function (S0013 Q3, step 7; D65).

A reference local (a `_root` local, parameters included) is live at a site if some path from the
site's return point reads it before writing it: the normal return and, inside `try_table`s, the
exceptional paths to the pads. One backward pass over the structured instruction list, the live
set a Python int in slot numbering (the caller gives each local's slot bit, so a v128 local before
it counts two slots, D41). Operand slots are not tracked here: they keep the validator's mask
(D65, a narrowing of Q3's `drop` rule).

The pass is sound for the machine code because of I5: after a site, a reference local's slot is
read only by a later wasm `local.get` (the lazy stack is flushed before every call, so no `LOCAL`
entry outlives a site; asserted in `_Func.site` and `gc.alloc`).

Loops: each loop's head set starts empty and the whole pass is repeated until no head set changes
(the sets only grow, so it ends; D65, plan D5). Exception edges: every site in the body of a
`try_table` joins the label sets of the clauses of every enclosing `try_table`, since a pad that
misses jumps to the enclosing one (`compiler.landing_pads`; plan D6); so do `throw` and
`throw_ref`, which jump to the pad. Cranelift computes the same sets with a worklist over basic
blocks (`cranelift/frontend/src/frontend/safepoints.rs`); structured wasm needs no CFG."""

from typing import Any

_OPEN = (0x02, 0x03, 0x04, 0x1F)  # block, loop, if, try_table
_NONE = (0x00, 0x0F, 0x12, 0x13, 0x15)  # unreachable, return, return_call*: nothing live after
_THROW = (0x08, 0x0A)  # throw, throw_ref: to the pad
# br_if, br_on_null, br_on_non_null, br_on_cast, br_on_cast_fail: the fall-through and the label
_BRANCH = {0x0D: 1, 0xD5: 1, 0xD6: 1, 0xFB18: 2, 0xFB19: 2}
_KEEP = frozenset((0x05, 0x0B, 0x0C, 0x0E, *_NONE, *_THROW, *_BRANCH))


def _label(ctl: list[list[int]], depth: int) -> int:
    """The live set at label `depth`: a block's continuation, a loop's head."""
    return ctl[-1 - depth][0]


def live(insns: list[Any], bit: dict[int, int], sites: Any) -> tuple[dict[int, int], int]:
    """pc -> the live bits at the instruction `pc` of every pc in `sites` (the set at its return
    point, joined with the enclosing pads' sets), and the number of passes. `bit` maps each
    reference local to its slot bit."""
    keep: list[tuple[int, Any, int]] = []  # (pc, ins, the opener's pc for else/end, else -1)
    opened: list[int] = []
    for pc, ins in enumerate(insns):
        op = ins[0]
        if op in _OPEN:
            opened.append(pc)
            keep.append((pc, ins, -1))
        elif op == 0x0B:  # the function's own end closes nothing
            keep.append((pc, ins, opened.pop() if opened else -1))
        elif op == 0x05:
            keep.append((pc, ins, opened[-1]))
        elif op in _KEEP or pc in sites or (op in (0x20, 0x21, 0x22) and ins[1] in bit):
            keep.append((pc, ins, -1))
    heads: dict[int, int] = {}  # a loop's pc -> its head set (its label's), from the last pass
    passes = 0
    while True:
        passes += 1
        out: dict[int, int] = {}
        grew = False
        ctl: list[list[int]] = []  # per open construct: [label set, H, else-arm start or -1]
        s = 0
        for pc, ins, o in reversed(keep):
            op = ins[0]
            if op == 0x0B:
                h = ctl[-1][1] if ctl else 0
                if o < 0:  # the function: a branch to it returns
                    ctl.append([0, 0, -1])
                elif insns[o][0] == 0x03:
                    ctl.append([heads.get(o, 0), h, -1])
                else:
                    if insns[o][0] == 0x1F:  # clause labels are counted outside the try_table
                        for clause in insns[o][2]:
                            h |= _label(ctl, clause[2])
                    ctl.append([s, h, -1])
            elif op == 0x05:
                ctl[-1][2], s = s, ctl[-1][0]  # the then-arm continues after the end
            elif op in _OPEN:
                c = ctl.pop()
                if op == 0x04:
                    s |= c[0] if c[2] < 0 else c[2]
                elif op == 0x03:
                    s |= heads.get(pc, 0)
                    grew = grew or s != heads.get(pc, 0)
                    heads[pc] = s
            elif op == 0x20:
                s |= bit[ins[1]]
            elif op == 0x21 or op == 0x22:  # tee reads its operand, not the local
                s &= ~bit[ins[1]]
            elif op == 0x0C:
                s = _label(ctl, ins[1])
            elif op in _BRANCH:
                s |= _label(ctl, ins[_BRANCH[op]])
            elif op == 0x0E:
                s = 0
                for d in (*ins[1], ins[2]):
                    s |= _label(ctl, d)
            elif op in _NONE:
                s = 0
            elif op in _THROW:
                s = ctl[-1][1]
            if pc in sites:
                s |= ctl[-1][1]
                out[pc] = s
        if not grew:
            return out, passes
