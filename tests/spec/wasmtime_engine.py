# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Wasmtime as a SpecEngine (docs/specs/S0001, "Wasmtime adapter"). Only file importing wasmtime.

Calls and global reads use wasmtime-py's raw C path: its `float` path quiets f32 signalling NaNs,
and it cannot convert v128 at all.
It needs private names of the pinned wasmtime-py, hence the pragma above.
"""

import ctypes
from collections.abc import Mapping, Sequence
from typing import Any, cast

import wasmtime
from wasmtime import _ffi as ffi
from wasmtime._func import enter_wasm

from tests.spec.engine import (
    ExternRef,
    FuncRef,
    Instance,
    LinkError,
    LoadError,
    Module,
    Trap,
    TrapCode,
    Value,
)


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [""])[0]


_V128 = ffi.wasmtime_wasm_valtype_v128()


def _kind(t: wasmtime.ValType) -> str:
    """str(t), but v128: wasm_valtype_kind aborts the process on it, so compare before asking.

    Upstream: https://github.com/bytecodealliance/wasmtime-py/issues/348,
    https://github.com/bytecodealliance/wasmtime/issues/15088 — drop this when they are fixed.
    """
    return "v128" if ffi.wasmtime_wasm_valtype_equal(t.ptr(), _V128) else str(t)


def _from_raw(store: wasmtime.Store, ty: str, raw: "ffi.wasmtime_val_t") -> Value:
    if ty == "i32":
        return raw.of.i32
    if ty == "i64":
        return raw.of.i64
    if ty == "f32":
        return raw.of.i32 & 0xFFFFFFFF
    if ty == "f64":
        return raw.of.i64 & 0xFFFFFFFFFFFFFFFF
    if ty == "v128":
        return int.from_bytes(bytes(raw.of.v128), "little")
    v = wasmtime.Val._from_raw(store, raw).value
    if v is None:
        return None
    return FuncRef(v) if isinstance(v, wasmtime.Func) else cast(ExternRef, v)


# The one Config of the oracle (S0001): every switch the claimed spec directories need, explicit.
CONFIG_SWITCHES = (
    *("wasm_multi_value", "wasm_reference_types", "wasm_bulk_memory", "wasm_tail_call"),
    *("wasm_function_references", "wasm_gc", "wasm_exceptions"),
    *("wasm_multi_memory", "wasm_memory64", "wasm_simd", "wasm_relaxed_simd"),
)


class WasmtimeEngine:
    def __init__(self) -> None:
        config = wasmtime.Config()
        for name in CONFIG_SWITCHES:
            setattr(config, name, True)
        self._engine = wasmtime.Engine(config)
        self._store = wasmtime.Store(self._engine)

    def load(self, wasm: bytes) -> Module:
        if not wasm.startswith(b"\0asm"):  # wasmtime-py would parse it as WAT
            raise LoadError("not a binary module")
        try:
            return wasmtime.Module(self._engine, wasm)
        except wasmtime.WasmtimeError as e:
            raise LoadError(_first_line(e)) from None

    def instantiate(self, module: Module, imports: Mapping[str, Instance]) -> Instance:
        externs: list[Any] = []
        for imp in cast(wasmtime.Module, module).imports:
            source = imports.get(imp.module)
            if source is None:
                raise LinkError(f"unknown import module {imp.module}")
            extern = cast(wasmtime.Instance, source).exports(self._store).get(imp.name or "")
            if extern is None:
                raise LinkError(f"unknown import {imp.module}.{imp.name}")
            externs.append(extern)
        try:
            return wasmtime.Instance(self._store, cast(wasmtime.Module, module), externs)
        except wasmtime.Trap as e:
            raise self._trap(e) from None
        except wasmtime.WasmtimeError as e:
            raise LinkError(_first_line(e)) from None

    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]:
        func = cast(wasmtime.Instance, instance).exports(self._store).get(name)
        if not isinstance(func, wasmtime.Func):
            raise LookupError(f"no exported function {name}")
        store = self._store
        ty = func.type(store)
        params = ty.params
        raw_args = (ffi.wasmtime_val_t * len(args))()
        for i, (t, a) in enumerate(zip(params, args, strict=True)):
            kind = _kind(t)
            if kind == "v128":
                raw_args[i].kind = ffi.WASMTIME_V128
                raw_args[i].of.v128 = (ctypes.c_uint8 * 16)(*cast(int, a).to_bytes(16, "little"))
            elif kind in ("i32", "f32"):
                raw_args[i].kind = ffi.WASMTIME_I32 if kind == "i32" else ffi.WASMTIME_F32
                raw_args[i].of.i32 = ctypes.c_int32(cast(int, a)).value
            elif kind in ("i64", "f64"):
                raw_args[i].kind = ffi.WASMTIME_I64 if kind == "i64" else ffi.WASMTIME_F64
                raw_args[i].of.i64 = ctypes.c_int64(cast(int, a)).value
            else:
                host = a.handle if isinstance(a, FuncRef) else a
                raw_args[i] = wasmtime.Val._convert_to_raw(store, t, host)
        results = ty.results
        raw_res = (ffi.wasmtime_val_t * len(results))()
        try:
            with enter_wasm(store) as trap:
                err: Any = ffi.wasmtime_func_call(
                    store._context(),
                    ctypes.byref(func._func),
                    raw_args,
                    len(args),
                    raw_res,
                    len(results),
                    trap,
                )
                if err:
                    raise wasmtime.WasmtimeError._from_ptr(err)
        except wasmtime.Trap as e:
            raise self._trap(e) from None
        finally:
            for i in range(len(args)):
                ffi.wasmtime_val_unroot(ctypes.byref(raw_args[i]))
        return [_from_raw(store, _kind(t), r) for t, r in zip(results, raw_res, strict=True)]

    def get_global(self, instance: Instance, name: str) -> Value:
        g = cast(wasmtime.Instance, instance).exports(self._store).get(name)
        if not isinstance(g, wasmtime.Global):
            raise LookupError(f"no exported global {name}")
        raw = ffi.wasmtime_val_t()
        ffi.wasmtime_global_get(self._store._context(), ctypes.byref(g._global), ctypes.byref(raw))
        return _from_raw(self._store, _kind(g.type(self._store).content), raw)

    @staticmethod
    def _trap(e: wasmtime.Trap) -> Exception:
        code = e.trap_code
        if code is None:
            return e
        return Trap(TrapCode[code.name], e.message)  # a code with no member: KeyError, as is
