"""The aarch64 backend (S0004), step 4a: only the frame, trap, call and poll methods of the D26
interface, which the ABI tests drive with hand-built functions (tests/jit/test_abi_aarch64.py);
the operations come in step 4b. Register roles in abi.py; pool x19-x24 (callee-saved for a host)
first, then x2-x8 (saved by the bridge)."""

from dataclasses import dataclass, field

from w2py.targets.base import TrapCode
from w2py.targets.jit.aarch64 import abi
from w2py.targets.jit.aarch64.abi import CTX, FRAME
from w2py.targets.jit.aarch64.encoder import FP, LR, SP, Asm, Label
from w2py.targets.jit.common.context import (
    HOST_BRIDGE,
    INTERRUPT,
    NSTACK_LIMIT,
    STATUS_INTERRUPT,
    VSTACK_LIMIT,
)

POOL = (19, 20, 21, 22, 23, 24, 2, 3, 4, 5, 6, 7, 8)


@dataclass
class Frame:
    pins: tuple[int, ...]
    size: int  # bytes of the native frame: record + pins, a multiple of 16
    unwind: Label
    vcheck: int  # position of the `movz/movk x16, #8 * frame_slots` pair
    traps: dict[int, Label] = field(default_factory=lambda: {})


class Backend:
    pool = POOL
    tos = 0

    def __init__(self) -> None:
        self.a = Asm()

    def new_label(self) -> Label:
        return self.a.label()

    def bind(self, label: Label) -> None:
        self.a.bind(label)

    def jump(self, label: Label) -> None:
        self.a.b(label)

    def here(self) -> int:
        return len(self.a.buf)

    def _add_k(self, d: int, n: int, k: int) -> None:
        """d = n + k (k may be negative); x16 for a constant that no add/sub immediate takes."""
        op, v = ("add", k) if k >= 0 else ("sub", -k)
        try:
            self.a.addsub_imm(op, d, n, v)
        except ValueError:
            self.a.mov_imm(16, k)
            self.a.addsub("add", d, n, 16)

    def _pins(self, f: Frame, load: bool) -> None:
        a, p = self.a, f.pins
        for i in range(0, len(p) - 1, 2):
            a.pair(load, p[i], p[i + 1], SP, 16 + 8 * i)
        if len(p) % 2:
            (a.ld if load else a.st)(p[-1], SP, 16 + 8 * (len(p) - 1))

    def func_begin(self, label: Label, pins: tuple[int, ...] = ()) -> Frame:
        """Landing pad, frame record + pins (sp stays 16-aligned), both stack checks, poll."""
        a = self.a
        assert all(r in POOL for r in pins)
        a.bind(label)
        a.bti("c")  # reached by blr from the entry and records, by br x16/x17 from tail calls
        f = Frame(pins, 16 + (len(pins) + 1) // 2 * 16, a.label(), 0)
        a.pair(False, FP, LR, SP, -f.size, "pre")
        a.mov(FP, SP)
        self._pins(f, False)
        a.ld(16, CTX, 8 * NSTACK_LIMIT)
        a.cmp(SP, 16)
        a.bcc("lo", self._stub(f, TrapCode.STACK_OVERFLOW))
        f.vcheck = len(a.buf)
        a.movw("movz", 16, 0)  # 8 * frame_slots, patched by func_end
        a.movw("movk", 16, 0, 16)
        a.addsub("add", 16, FRAME, 16)
        a.ld(17, CTX, 8 * VSTACK_LIMIT)
        a.cmp(16, 17)
        a.bcc("hi", self._stub(f, TrapCode.STACK_OVERFLOW))
        self.poll(f)
        return f

    def func_end(self, f: Frame, frame_slots: int) -> None:
        """Patch the value-stack check, emit the cold trap stubs and the epilogue (`unwind`, with
        w0 intact)."""
        a = self.a
        k = Asm()
        k.movw("movz", 16, 8 * frame_slots & 0xFFFF)
        k.movw("movk", 16, 8 * frame_slots >> 16, 16)
        a.buf[f.vcheck : f.vcheck + 8] = k.buf
        for code, label in f.traps.items():
            a.bind(label)
            a.mov_imm(0, code, wide=False)
            a.b(f.unwind)
        a.bind(f.unwind)
        self._pins(f, True)
        a.pair(True, FP, LR, SP, f.size, "post")
        a.ret()

    def _stub(self, f: Frame, code: int) -> Label:
        if code not in f.traps:
            f.traps[code] = self.a.label()
        return f.traps[code]

    def poll(self, f: Frame) -> None:
        """At function entry and loop headers (D14, LESSONS 8)."""
        self.a.ld(16, CTX, 8 * INTERRUPT)
        self.a.cbz(16, self._stub(f, STATUS_INTERRUPT), nz=True)

    def trap(self, f: Frame, code: int) -> None:
        self.a.b(self._stub(f, code))

    def ret_ok(self, f: Frame) -> None:
        self.a.mov_imm(0, 0, wide=False)
        self.a.b(f.unwind)

    def call_direct(self, f: Frame, label: Label, shift_slots: int) -> None:
        """The callee's frame starts `shift_slots` above ours (frame overlap). A non-zero status
        unwinds this frame too."""
        if shift_slots:
            self._add_k(FRAME, FRAME, 8 * shift_slots)
        self.a.bl(label)
        if shift_slots:
            self._add_k(FRAME, FRAME, -8 * shift_slots)
        self.a.cbz(0, f.unwind, wide=False, nz=True)

    def host_thunk(self, word: int) -> int:
        at = len(self.a.buf)
        abi.host_thunk(self.a, word)
        return at

    def call_host(self, f: Frame, word: int, slot: int) -> None:
        """Call the libcall in context word `word` through the bridge; its arguments and results
        are slots `slot`, `slot + 1`, ... of this frame."""
        a = self.a
        self._add_k(1, FRAME, 8 * slot)
        a.mov_imm(0, word, wide=False)
        a.ld(16, CTX, 8 * HOST_BRIDGE)
        a.blr(16)
        abi.reload_memory(a)  # memory.grow may have run
        a.cbz(0, f.unwind, wide=False, nz=True)
