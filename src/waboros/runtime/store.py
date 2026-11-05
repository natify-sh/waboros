"""The per-store state shared by the runtime and its target instances (docs/specs/S0002)."""

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from waboros.targets.base import TargetInstance

_CLAIM = threading.Lock()  # the first claim of any store: check and set as one step


class StoreState:
    """One thread per store: the owner is recorded on the first entry (atomically); nesting is
    allowed. A JIT grow or instantiation holds a claim while it runs (S0009)."""

    def __init__(self, max_wasm_stack: int = 0) -> None:
        self.owner: int | None = None
        self.entered = False  # an entry has recorded the owner: it stays (S0002)
        self.depth = 0  # entries into wasm code that are active (wasm -> host -> wasm nests)
        self.max_wasm_stack = max_wasm_stack  # 0: the target's default
        self.instances: list[TargetInstance] = []
        self.pending: BaseException | None = None  # a host exception unwinding through JIT frames
        self.target_state: object = None  # the target's per-store data (JIT: the value stack)
        self.interrupt_lock = threading.Lock()  # interrupt() vs a target consuming the request

    def claim(self) -> bool:
        """Make the calling thread the owner if the store has none (True: this call did); raise
        if another thread owns it. The first claim is atomic (two threads cannot both win)."""
        me = threading.get_ident()
        if self.owner == me:
            return False
        with _CLAIM:
            took = self.owner is None
            if took:
                self.owner = me
        if not took:
            raise RuntimeError("a store is used from one thread only")
        return True

    def release(self, took: bool) -> None:
        """End a claim made outside an entry (JIT grow, instantiation): only an entry records the
        owner for good, so a store built on one thread can still run on another."""
        if took and not self.entered:
            self.owner = None

    def enter(self) -> None:
        self.claim()
        self.entered = True
        self.depth += 1

    def leave(self) -> None:
        self.depth -= 1

    def interrupt(self) -> None:
        """Any thread: the store's running code traps with INTERRUPT at its next poll (JIT, S0003;
        pyc: later)."""
        with self.interrupt_lock:
            for inst in list(self.instances):
                getattr(inst, "interrupt", lambda: None)()
