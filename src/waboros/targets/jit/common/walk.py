"""The frame walk of a store's JIT code (docs/specs/S0013, "The walk"): from the store's exit record
(context word 14, written by the bridge) through every wasm frame by its return address and the
site numbers of `Compiled.stackmaps`, across nested entries by the block each entry trampoline
keeps. An unknown return address or a frame base that disagrees with its entry is an internal
error (`RuntimeError`), never a guess.

The check mode (`WABOROS_JIT_WALK_CHECK=1`, tests only; read when an instance is created) walks
at every host call and libcall: `check`."""

import bisect
import ctypes
import os
from typing import Any

from waboros.targets.jit.common import gc

RESTORE_RET_AT, RESTORE_UP = 8, 16  # tail_switch's restore frame: context, real return address
Frame = tuple[Any, tuple[int, int, int, int, int], int, int]  # instance, site, sp, base


def checking() -> bool:
    return os.environ.get("WABOROS_JIT_WALK_CHECK") == "1"


def _word(addr: int) -> int:
    return ctypes.c_uint64.from_address(addr).value


def _site(store: Any, st: Any, pc: int) -> tuple[Any, tuple[int, int, int, int, int]]:
    """The instance whose code holds `pc` and the site whose return address it is. Code ranges
    and site tables are built on first use (cached by the number of instances, which only grow)."""
    cache = st.walk_ranges
    if cache is None or cache[0] != len(store.instances):
        insts = sorted(
            (i for i in store.instances if getattr(i, "stackmaps", None) is not None),
            key=lambda i: i.code.addr,
        )
        cache = st.walk_ranges = (len(store.instances), [i.code.addr for i in insts], insts)
    k = bisect.bisect_right(cache[1], pc) - 1
    inst = cache[2][k] if k >= 0 else None
    if inst is None or pc >= inst.code.addr + inst.code.size:
        raise RuntimeError(f"JIT walk: return address {pc:#x} is in no code of the store")
    if inst.walk_sites is None:
        inst.walk_sites = gc.decode(inst.stackmaps)
    site = inst.walk_sites.get(pc - inst.code.addr)
    if site is None:
        raise RuntimeError(f"JIT walk: return address {pc:#x} is not a call site")
    return inst, site


def walk(store: Any, st: Any, tramp: Any) -> list[tuple[list[Frame], int]]:
    """Segments, innermost first: (frames innermost first, the frame the entry was given). No
    stack word is read outside [the store's native limit - 4 KiB, the thread's stack top): a frame
    sits above the native limit but for its pushes, and the walker runs below every frame."""
    from waboros.targets.jit.common.instance import stack_top  # (instance imports this module)

    lo, hi = st.nlimit - 4096, stack_top()
    if hi is None:
        raise RuntimeError("JIT walk: the thread's stack bounds are unknown")

    def read(addr: int) -> int:
        if not lo <= addr <= hi - 8:
            raise RuntimeError(f"JIT walk: stack address {addr:#x} outside [{lo:#x}, {hi:#x})")
        return _word(addr)

    sp, pc, base = (_word(ctypes.addressof(st.exit) + 8 * i) for i in range(3))
    segments: list[tuple[list[Frame], int]] = []
    frames: list[Frame] = []
    if not sp:
        return segments
    inst, site = _site(store, st, pc)
    while True:
        frames.append((inst, site, sp, base))
        _, ret_at, up, _, _ = site
        pc, nsp = read(sp + ret_at), sp + up
        if pc == tramp.restore:  # one restore frame at most; the tail-called frame keeps the base
            pc, nsp = read(nsp + RESTORE_RET_AT), nsp + RESTORE_UP
            if pc == tramp.restore:
                raise RuntimeError("JIT walk: a restore frame returns to another")
        if nsp <= sp:
            raise RuntimeError(f"JIT walk: stack pointer {nsp:#x} not above {sp:#x}")
        if pc == tramp.entry_ret:
            saved = nsp + tramp.saved
            if read(saved + 24) != base:
                raise RuntimeError(f"JIT walk: frame base {base:#x} is not its entry's")
            segments.append((frames, base))
            if len(segments) > st.active:
                raise RuntimeError("JIT walk: more segments than active entries")
            sp, pc, base = (read(saved + 8 * i) for i in range(3))
            if not sp:
                return segments
            if sp <= saved:  # the enclosing host call's frames are outside this entry's
                raise RuntimeError(f"JIT walk: saved stack pointer {sp:#x} not above {saved:#x}")
            frames = []
            inst, site = _site(store, st, pc)
            continue
        inst, site = _site(store, st, pc)
        sp, base = nsp, base - 8 * site[0]


def check(store: Any, st: Any, tramp: Any, slots: int) -> None:
    """At a host call (check mode): the record was written for this call, the walk crosses exactly
    the active entries, each segment ends at the frame the runtime gave its entry, and every
    marked slot holds a valid reference word (`valid`)."""
    sp, _, frame = st.exit
    if not sp or frame > slots:
        raise RuntimeError(f"JIT walk check: exit record not written ({sp:#x}, {frame:#x})")
    segments = walk(store, st, tramp)
    entries = [st.vstack.addr, *st.walk_vsp]  # outermost first
    found = [b for _, b in reversed(segments)]
    if found != entries:
        raise RuntimeError(f"JIT walk check: segments at {found}, entries at {entries}")
    for frames, _ in segments:
        for _, (_, _, _, n, bits), _, base in frames:
            if bits and not st.vstack.addr <= base <= st.vtop - 8 * n:
                raise RuntimeError(f"JIT walk check: frame base {base:#x} outside the value stack")
            for k in range(n):
                if bits >> k & 1 and not valid(st, _word(base + 8 * k)):
                    raise RuntimeError(f"JIT walk check: slot {k} of {base:#x} not a reference")


def valid(st: Any, w: int) -> bool:
    """`w` is a reference word of the any or extern hierarchy in store `st` (S0013 3b): null, an i31
    `(v << 1) | 1`, a host value `(h << 2) | 2` with a live handle h, or an object of the heap."""
    if w == 0:
        return True
    if w & 3 == 2:
        return 1 <= w >> 2 <= len(st.externs)
    # an i31 or an object only where GC code ran: a store without a heap keeps 3a's check
    return st.heap is not None and w < 1 << 32 and (w & 1 == 1 or st.heap.is_object(w))
