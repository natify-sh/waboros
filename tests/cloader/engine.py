# pyright: basic
"""The S0001 engines `cloader-<target>` (S0017 "The harness"): the Python compiler of `<target>`
writes a fat file, the C loader loads, instantiates and calls it in-process through the shim
(`shim.py`). `tests.spec.run` runs each `.wast` in its own child, so a C crash costs one file.

Step 2: explicit bounds (guard files are step 4), modules without imports (step 3), numbers, v128,
null references and the funcrefs the C loader returns; a host reference is step 3."""

import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, cast

from tests.cloader import shim
from tests.spec.engine import (
    FuncRef,
    Instance,
    LinkError,
    Module,
    OpaqueRef,
    SpecEngine,
    Trap,
    TrapCode,
    Unsupported,
    Value,
)
from tests.spec.target_engine import TargetEngine
from waboros.frontend.binary import F32, I32, I64
from waboros.frontend.types import ANY, EXTERN, FUNC, isref, nn, top
from waboros.runtime import link
from waboros.targets import base
from waboros.targets.jit.common import artifact as A

V128 = 0x7B
M32, M64 = (1 << 32) - 1, (1 << 64) - 1
STEP3_REFS = "cloader step 2: host references (S0017 step 3)"


@contextmanager
def _explicit():
    """Explicit bounds while compiling (step 2: the C loader has no fault handler yet)."""
    old = os.environ.get("WABOROS_JIT_BOUNDS")
    os.environ["WABOROS_JIT_BOUNDS"] = "explicit"
    try:
        yield
    finally:
        if old is None:
            del os.environ["WABOROS_JIT_BOUNDS"]
        else:
            os.environ["WABOROS_JIT_BOUNDS"] = old


class _Module:
    def __init__(self, mod: Any, handle: int) -> None:
        self.mod, self.handle = mod, handle


class _Instance:
    def __init__(self, mod: Any, handle: int) -> None:
        self.mod, self.handle = mod, handle


def _width(ts: Sequence[Any]) -> int:
    return sum(2 if t == V128 else 1 for t in ts)


def _to_raw(t: Any, v: Value) -> list[int]:
    if isref(t):
        if v is None:
            return [0]
        if isinstance(v, FuncRef) and isinstance(v.handle, int):  # a record this loader returned
            return [v.handle]
        raise NotImplementedError(STEP3_REFS)
    assert isinstance(v, int)
    if t == V128:
        return [v & M64, v >> 64 & M64]
    return [v & (M32 if t in (I32, F32) else M64)]


def _from_raw(mod: Any, t: Any, w: list[int]) -> Value:
    if isref(t):
        r = w[0]
        if not r:
            return None
        h = top(mod, nn(t)[1])
        if h == FUNC:
            return FuncRef(r)
        if r & 1 and h in (ANY, EXTERN):  # an i31 (seen as an externref: opaque, as Python)
            return OpaqueRef("i31" if h == ANY else "extern")
        raise NotImplementedError(STEP3_REFS)
    if t == V128:
        return w[0] | w[1] << 64
    if t in (I32, I64):
        m = M32 if t == I32 else M64
        v = w[0] & m
        return v - ((v & (m + 1 >> 1)) << 1)
    return w[0] & (M32 if t == F32 else M64)


def _error(e: shim.CError) -> Exception:
    if e.code == shim.E_TRAP:
        return Trap(TrapCode[base.TrapCode(e.trap).name], e.msg)
    if e.code == shim.E_LINK:
        return LinkError(e.msg)
    if e.code == shim.E_RESOURCE:
        return MemoryError(e.msg)
    return RuntimeError(e.msg)


class CloaderEngine:
    """`cloader-<target>`: one C engine with the test compiler of `<target>`, one store."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.py = TargetEngine(target)  # the front end and the compiler (TargetEngine.load)
        self.eng = shim.Engine(target)
        self.store = shim.Store(self.eng)

    def load(self, wasm: bytes) -> Module:
        with _explicit():
            m = self.py.load(wasm)
            if isinstance(m, Unsupported):
                return m
            c = cast(Any, m).compiled
            fat = A.write(c, target=self.target, wasm=wasm)
            try:
                return _Module(cast(Any, m).mod, self.eng.load_module(fat))
            except shim.CError as e:
                raise RuntimeError(f"the C loader refused the file: {e}") from None

    def instantiate(self, module: Module, imports: Mapping[str, Instance]) -> Instance:
        if isinstance(module, Unsupported):
            raise Unsupported(str(module))
        m = cast(_Module, module)
        try:
            return _Instance(m.mod, self.store.instantiate(m.handle))
        except shim.CError as e:
            raise _error(e) from None

    def _export(self, inst: _Instance, name: str, kind: int) -> tuple[shim.Extern, int]:
        idx = next((i for n, k, i in inst.mod["exports"] if n == name and k == kind), None)
        x = self.store.export(inst.handle, name)
        if idx is None or x is None or x.kind != kind:
            raise LookupError(f"no export {name}")
        return x, idx

    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]:
        inst = cast(_Instance, instance)
        x, idx = self._export(inst, name, shim.EXTERN_FUNC)
        ps, rs = link.func_type(inst.mod, idx)
        raw = [w for t, a in zip(ps, args, strict=True) for w in _to_raw(t, a)]
        try:
            out = self.store.call(x, raw, max(_width(ps), _width(rs)))
        except shim.CError as e:
            raise _error(e) from None
        res, at = [], 0
        for t in rs:
            n = 2 if t == V128 else 1
            res.append(_from_raw(inst.mod, t, out[at : at + n]))
            at += n
        return res

    def get_global(self, instance: Instance, name: str) -> Value:
        inst = cast(_Instance, instance)
        x, idx = self._export(inst, name, shim.EXTERN_GLOBAL)
        return _from_raw(
            inst.mod, link.global_type(inst.mod, idx)[0], list(self.store.global_get(x))
        )


def engines() -> dict[str, Callable[[], SpecEngine]]:
    """`cloader-<target>` for each JIT target of this host the shim can run (the library built)."""
    if shim.LIB is None:
        return {}
    names = [t for t in ("jit-x86_64", "jit-sljit-x86_64") if t in base.TARGETS]
    if shim.MACHINE != 62:
        return {}

    def make(t: str) -> Callable[[], SpecEngine]:
        return lambda: cast(SpecEngine, CloaderEngine(t))

    return {f"cloader-{t}": make(t) for t in names}
