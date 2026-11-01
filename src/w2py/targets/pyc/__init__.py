# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false
"""The `pyc` target: WebAssembly to Python source, behind the S0002 `Target` interface."""

import struct
from collections.abc import Callable, Mapping, Sequence
from types import CodeType
from typing import Any, Literal, cast

from w2py.frontend.binary import F32, F64, parse
from w2py.frontend.validate import validate, valtype_key
from w2py.runtime.link import func_type, global_type
from w2py.runtime.store import StoreState
from w2py.targets.base import Extern, HostError, HostFunc, Raw, Trap, TrapCode, ValidModule
from w2py.targets.pyc import runtime as _rt
from w2py.targets.pyc.codegen import generate

__all__ = ["PycTarget", "Trap", "load", "translate"]

_KIND: dict[int, Literal["func", "table", "memory", "global"]] = {
    0: "func",
    1: "table",
    2: "memory",
    3: "global",
}
_f32_i32 = cast(Callable[[int], float], _rt._f32_i32)
_f64_i64 = cast(Callable[[int], float], _rt._f64_i64)
_i32_f32 = cast(Callable[[float], int], _rt._i32_f32)
_i64_f64 = cast(Callable[[float], int], _rt._i64_f64)


def _from_raw(ty: Any, r: Any) -> Any:
    """Raw -> the value `pyc` code uses: f32/f64 become floats. References pass as they are."""
    return _f32_i32(r) if ty == F32 else _f64_i64(r) if ty == F64 else r


def _to_raw(ty: Any, v: Any) -> Any:
    return _i32_f32(v) if ty == F32 else _i64_f64(v) if ty == F64 else v


def _prepare(wasm: bytes) -> ValidModule:
    mod = parse(wasm)
    validate(mod)  # also decodes the bodies into mod["insns"]
    return mod


def translate(wasm: bytes) -> str:
    """Translate a wasm binary to Python source. The source imports its helpers from
    `w2py.targets.pyc.runtime`."""
    return generate(_prepare(wasm))


def _run(store: StoreState, fn: Callable[..., Any], *args: Any) -> Any:
    """Where control enters wasm code: Python errors that mean a trap become `Trap` (S0002); a
    host exception comes out as itself at the outermost entry."""
    store.enter()
    try:
        return fn(*args)
    except HostError as e:
        if store.depth == 1:
            raise e.exc from None
        raise
    except Trap:
        raise
    except (IndexError, struct.error):
        raise Trap(TrapCode.MEMORY_OUT_OF_BOUNDS, "out of bounds memory access") from None
    except ZeroDivisionError:
        raise Trap(TrapCode.INTEGER_DIVISION_BY_ZERO, "integer divide by zero") from None
    except RecursionError:
        raise Trap(TrapCode.STACK_OVERFLOW, "call stack exhausted") from None
    finally:
        store.leave()


def load(wasm: bytes, imports: Mapping[str, Mapping[str, Callable[..., Any]]]) -> dict[str, Any]:
    """Translate, compile and instantiate (own store); exports by name, functions as plain
    callables on `pyc` values (memory as a `bytearray`)."""
    code = compile(translate(wasm), "<w2py-pyc>", "exec", optimize=2)
    namespace: dict[str, Any] = {"__name__": "w2py_pyc_module"}
    exec(code, namespace)
    store = StoreState()
    exports = cast(dict[str, Any], _run(store, namespace["instantiate"], imports))
    return {n: _entry(store, v) if callable(v) else v for n, v in exports.items()}


def _entry(store: StoreState, f: Callable[..., Any]) -> Callable[..., Any]:
    return lambda *a: _run(store, f, *a)


class _Compiled:
    def __init__(self, module: ValidModule, code: CodeType) -> None:
        self.module = module
        self.code = code


class PycInstance:
    def __init__(
        self,
        compiled: _Compiled,
        namespace: dict[str, Any],
        exports: dict[str, Any],
        store: StoreState,
    ) -> None:
        self.module = compiled.module
        self.ns = namespace
        self.exports = exports
        self.store = store

    def call(self, func_index: int, args: Sequence[Raw]) -> list[Raw]:
        ps, rs = func_type(self.module, func_index)
        name = f"I{func_index}" if func_index < self._nimp() else f"f{func_index}"
        raw = [_from_raw(t, a) for t, a in zip(ps, args, strict=True)]
        res = _run(self.store, self.ns[name], *raw)
        vals = [] if not rs else [res] if len(rs) == 1 else list(res)
        return [_to_raw(t, v) for t, v in zip(rs, vals, strict=True)]

    def _nimp(self) -> int:
        return sum(1 for im in self.module["imports"] if im[0] == "func")

    def export(self, name: str) -> Extern:
        for n, kind, _ in self.module["exports"]:
            if n == name:
                return Extern(_KIND[kind], "pyc", self.exports[name])
        raise LookupError(f"no export {name}")

    def limits(self, name: str) -> tuple[int, int | None]:
        ex: Any = self.exports[name]
        if isinstance(ex, _rt._Table):
            return len(ex.t), ex.max
        return len(ex) >> 16, ex.max

    def global_get(self, global_index: int) -> Raw:
        ty = global_type(self.module, global_index)[0]
        cell = self.ns.get(f"C{global_index}")
        return _to_raw(ty, cell.v if cell is not None else self.ns[f"G{global_index}"])


def _host_adapter(h: HostFunc, ps: list[int], rs: list[int], sig: str) -> Callable[..., Any]:
    """A `HostFunc` as a function on `pyc` values; `.s` lets it sit in a table."""

    def w(*a: Any) -> Any:
        res = h([_to_raw(t, v) for t, v in zip(ps, a, strict=True)])
        vals = [_from_raw(t, r) for t, r in zip(rs, res, strict=True)]
        return None if not rs else vals[0] if len(rs) == 1 else tuple(vals)

    w.s = sig  # pyright: ignore[reportFunctionMemberAccess]
    return w


class PycTarget:
    name = "pyc"
    version = 1

    def probe(self) -> str | None:
        return None

    def supports(self, module: ValidModule) -> str | None:
        return None  # a module the validator does not cover may run: a Python exception is safe

    def compile(self, module: ValidModule) -> object:
        return _Compiled(module, compile(generate(module), "<w2py-pyc>", "exec", optimize=2))

    def instantiate(
        self, compiled: object, imports: Sequence[Extern], store: StoreState
    ) -> PycInstance:
        c = cast(_Compiled, compiled)
        namespace: dict[str, Any] = {"__name__": "w2py_pyc_module"}
        exec(c.code, namespace)
        wired: dict[str, dict[str, Any]] = {}
        for im, ext in zip(c.module["imports"], imports, strict=True):
            val = ext.value
            if im[0] == "func":
                if ext.owner is None:
                    ps, rs = c.module["types"][im[3]]
                    ty = c.module["types"]
                    sig = ">".join(",".join(str(valtype_key(ty, t)) for t in ts) for ts in (ps, rs))
                    val = _host_adapter(cast(HostFunc, val), ps, rs, sig)
            wired.setdefault(im[1], {})[im[2]] = val
        exports = cast(dict[str, Any], _run(store, namespace["instantiate"], wired))
        inst = PycInstance(c, namespace, exports, store)
        store.instances.append(inst)
        return inst
