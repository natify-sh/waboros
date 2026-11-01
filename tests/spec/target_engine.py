# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Every w2py target as a SpecEngine (docs/specs/S0001): a thin adapter over S0002 (select, compile,
instantiate, call). `TargetEngine(name)`: a target name (`pyc`, `jit-x86_64`, ...) or `auto` (D25).

A valid module the target cannot compile (`Unsupported`, D25: no fallback) loads as a placeholder;
instantiating it raises the S0001 `Unsupported`, so its commands fail `unsupported: <feature>` and
never pass.

Values: i32/i64 are signed at the interface and unsigned `Raw` below; f32/f64 are bit patterns at
both. References: null is None, an externref is the runner's ExternRef, a funcref is the function
object (a `pyc` reference passes through the `Raw` slot as it is until stage 2).
"""

import struct
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

from tests.spec.engine import (
    FuncRef,
    Instance,
    LinkError,
    LoadError,
    Module,
    Trap,
    TrapCode,
    Unsupported,
    Value,
)
from w2py.frontend.binary import F32, F64, I32, I64, ImplLimit, Malformed, parse
from w2py.frontend.validate import Invalid, validate
from w2py.runtime import link
from w2py.runtime.store import StoreState
from w2py.targets import base

_PARSE_ERRORS = (AssertionError, IndexError, ValueError, struct.error, UnicodeDecodeError)
_MASK = {
    I32: 0xFFFFFFFF,
    I64: 0xFFFFFFFFFFFFFFFF,
    F32: 0xFFFFFFFF,
    F64: 0xFFFFFFFFFFFFFFFF,
    0x7B: (1 << 128) - 1,  # v128
}


class _Module:
    def __init__(self, mod: base.ValidModule, target: base.Target, compiled: object) -> None:
        self.mod = mod
        self.target = target
        self.compiled = compiled


class _Providers(Mapping[str, link.Provider]):
    """The runner's registry seen as link providers, looked up only by name (the runner makes
    `spectest` on first lookup)."""

    def __init__(self, imports: Mapping[str, Instance]) -> None:
        self.imports = imports

    def __getitem__(self, key: str) -> link.Provider:
        inst = self.imports[key]
        if isinstance(inst, Unsupported):
            raise Unsupported(str(inst))
        return cast(_Instance, inst).provider

    def __iter__(self) -> Iterator[str]:
        return iter(self.imports)

    def __len__(self) -> int:
        return len(self.imports)


class _Instance:
    def __init__(self, module: base.ValidModule, instance: base.TargetInstance) -> None:
        self.provider = link.Provider(module, instance)


def _isref(ty: Any) -> bool:
    return ty.__class__ is tuple or ty in (0x70, 0x6F)


def _to_raw(ty: Any, v: Value) -> Any:
    if _isref(ty):  # a reference: null, ExternRef or the function
        return v.handle if isinstance(v, FuncRef) else v
    if not isinstance(v, int):
        raise NotImplementedError("reference values unsupported")
    return v & _MASK[ty]


def _from_raw(ty: Any, r: Any) -> Value:
    if _isref(ty):
        if (ty[1] == -17) if ty.__class__ is tuple else (ty == 0x6F):
            return cast(Value, r)
        return None if r is None else FuncRef(r)
    if ty in (I32, I64):
        return cast(int, r) - ((r & (_MASK[ty] + 1 >> 1)) << 1)
    return cast(int, r)


def _trap(e: base.Trap) -> Exception:
    return Trap(TrapCode[e.code.name], str(e))


class TargetEngine:
    def __init__(self, name: str) -> None:
        self.order = base.target_order(name)
        self.store = StoreState()

    def load(self, wasm: bytes) -> Module:
        try:
            try:
                mod = parse(wasm)
            except (ImplLimit, NotImplementedError) as e:  # not a verdict on the module
                raise LoadError(f"{type(e).__name__}: {e}") from None
            validate(mod)
            try:
                target = base.select(mod, self.order)
                compiled = target.compile(mod)  # NotImplementedError is a target gap
            except base.Unsupported as e:
                return Unsupported(e.feature)
        except Malformed as e:
            raise LoadError(str(e), "malformed") from None
        except Invalid as e:
            raise LoadError(str(e), "invalid") from None
        except _PARSE_ERRORS as e:
            raise LoadError(f"{type(e).__name__}: {e}") from None
        return _Module(mod, target, compiled)

    def instantiate(self, module: Module, imports: Mapping[str, Instance]) -> Instance:
        if isinstance(module, Unsupported):
            raise Unsupported(str(module))
        m = cast(_Module, module)
        providers = _Providers(imports)
        try:
            externs = link.resolve(m.mod, providers)
            return _Instance(m.mod, m.target.instantiate(m.compiled, externs, self.store))
        except link.LinkError as e:
            raise LinkError(str(e)) from None
        except base.Trap as e:
            raise _trap(e) from None

    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]:
        mod, inst = cast(_Instance, instance).provider
        idx = next((i for n, k, i in mod["exports"] if n == name and k == 0), None)
        if idx is None:
            raise LookupError(f"no exported function {name}")
        ps, rs = link.func_type(mod, idx)
        raw = [_to_raw(t, a) for t, a in zip(ps, args, strict=True)]
        try:
            res = inst.call(idx, raw)
        except base.Trap as e:
            raise _trap(e) from None
        return [_from_raw(t, v) for t, v in zip(rs, res, strict=True)]

    def get_global(self, instance: Instance, name: str) -> Value:
        mod, inst = cast(_Instance, instance).provider
        idx = next((i for n, k, i in mod["exports"] if n == name and k == 3), None)
        if idx is None:
            raise LookupError(f"no exported global {name}")
        return _from_raw(link.global_type(mod, idx)[0], inst.global_get(idx))
