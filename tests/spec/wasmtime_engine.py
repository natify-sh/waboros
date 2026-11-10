# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Wasmtime as a SpecEngine (docs/specs/S0001, "Wasmtime adapter"). Only file importing wasmtime.

Calls and global reads use wasmtime-py's raw C path: its `float` path quiets f32 signalling NaNs,
and it cannot convert v128 or an anyref at all, and its `ValType` aborts the process on v128 and on
a concrete reference type. Parameter types are therefore read with the C API's `wasmtime_valtype_t`
(never `wasm_valtype_kind`), and anyref values are classified with `wasmtime_anyref_*`.
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
    HostRef,
    Instance,
    LinkError,
    LoadError,
    Module,
    OpaqueRef,
    Trap,
    TrapCode,
    Value,
)


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [""])[0]


# wasmtime_valtype_t.kind 0..4 are the numbers; 5 is a reference, told apart by its heap type kind
# (extern 0-1, func 2-4, any 5-12, exn 13 and up), measured on wasmtime-py 50.0.1
NUMBERS = ("i32", "i64", "f32", "f64", "v128")
ANYREF, EXNREF = 7, 8  # wasmtime_valkind_t values the bindings have no constant for
CONVERT_WAT = """(module
  (func (export "internalize") (param externref) (result anyref)
    local.get 0 any.convert_extern)
  (func (export "externalize") (param anyref) (result externref)
    local.get 0 extern.convert_any))"""


def _kind(vt: Any) -> str:
    """The kind of a `wasm_valtype_t`, without wasm_valtype_kind (it aborts the process on v128 and
    on a concrete reference type; upstream: wasmtime-py#348, wasmtime#15088)."""
    t = ffi.wasmtime_valtype_t()
    ffi.wasmtime_valtype_new(vt, ctypes.byref(t))
    kind, heap = cast(int, t.kind), cast(int, t.reftype.heaptype.kind)
    ffi.wasmtime_valtype_delete(ctypes.byref(t))
    if kind < len(NUMBERS):
        return NUMBERS[kind]
    return (
        "externref" if heap < 2 else "funcref" if heap < 5 else "anyref" if heap < 13 else "exnref"
    )


def _signature(store: wasmtime.Store, func: wasmtime.Func) -> tuple[list[str], int]:
    """The parameter kinds and the number of results, from the C API (never `func.type()`)."""
    ft = ffi.wasmtime_func_type(store._context(), ctypes.byref(func._func))
    params = ffi.wasm_functype_params(ft).contents
    kinds = [_kind(params.data[i]) for i in range(params.size)]
    nresults = ffi.wasm_functype_results(ft).contents.size
    ffi.wasm_functype_delete(ft)
    return kinds, nresults


# The one Config of the oracle (S0001): every switch the claimed spec directories need, explicit.
CONFIG_SWITCHES = (
    *("wasm_multi_value", "wasm_reference_types", "wasm_bulk_memory", "wasm_tail_call"),
    *("wasm_function_references", "wasm_gc", "wasm_exceptions"),
    *("wasm_multi_memory", "wasm_memory64", "wasm_simd", "wasm_relaxed_simd"),
    *("wasm_relaxed_simd_deterministic", "wasm_wide_arithmetic"),
)


class WasmtimeEngine:
    def __init__(self) -> None:
        config = wasmtime.Config()
        for name in CONFIG_SWITCHES:
            setattr(config, name, True)
        self._engine = wasmtime.Engine(config)
        self._store = wasmtime.Store(self._engine)
        self._convert: Mapping[str, Any] | None = None  # exports of CONVERT_WAT, made when needed

    def _converter(self, name: str) -> wasmtime.Func:
        if self._convert is None:
            module = wasmtime.Module(self._engine, bytes(wasmtime.wat2wasm(CONVERT_WAT)))
            self._convert = wasmtime.Instance(self._store, module, []).exports(self._store)
        return cast(wasmtime.Func, self._convert[name])

    def _host_ref(self, n: int) -> "ffi.wasmtime_val_t":
        """An anyref of the host value n: the C API cannot make one, any.convert_extern does."""
        arg = (ffi.wasmtime_val_t * 1)()
        arg[0] = wasmtime.Val.externref(ExternRef(n))._new_raw(self._store)
        try:
            return self._invoke(self._converter("internalize"), arg, 1)[0]
        finally:
            ffi.wasmtime_val_unroot(ctypes.byref(arg[0]))

    def _from_raw(self, ty: int, raw: "ffi.wasmtime_val_t") -> Value:
        store = self._store
        if ty == ffi.WASMTIME_I32.value:
            return raw.of.i32
        if ty == ffi.WASMTIME_I64.value:
            return raw.of.i64
        if ty == ffi.WASMTIME_F32.value:
            return raw.of.i32 & 0xFFFFFFFF
        if ty == ffi.WASMTIME_F64.value:
            return raw.of.i64 & 0xFFFFFFFFFFFFFFFF
        if ty == ffi.WASMTIME_V128.value:
            return int.from_bytes(bytes(raw.of.v128), "little")
        if ty == ffi.WASMTIME_FUNCREF.value:
            v = wasmtime.Val._from_raw(store, raw).value
            return None if v is None else FuncRef(v)
        if ty == ffi.WASMTIME_EXTERNREF.value:
            if raw.of.externref.store_id == 0:
                return None
            v = wasmtime.Val._from_raw(store, raw).value
            return OpaqueRef("extern") if v is None else cast(ExternRef, v)  # not made by the host
        if ty == EXNREF and raw.of.exnref.store_id == 0:
            return None
        if ty != ANYREF:
            raise LookupError(f"unsupported result kind {ty}")
        ctx, ref = store._context(), ctypes.byref(raw.of.anyref)
        try:
            if raw.of.anyref.store_id == 0:
                return None
            if ffi.wasmtime_anyref_i31_get_u(ctx, ref, ctypes.byref(ctypes.c_uint32())):
                return OpaqueRef("i31")  # wasmtime_anyref_is_i31 answers true for every anyref
            if ffi.wasmtime_anyref_is_struct(ctx, ref):
                return OpaqueRef("struct")
            if ffi.wasmtime_anyref_is_array(ctx, ref):
                return OpaqueRef("array")
            arg = (ffi.wasmtime_val_t * 1)()  # a host value: extern.convert_any gives it back
            arg[0] = raw
            ext = self._invoke(self._converter("externalize"), arg, 1)[0]
            host = self._from_raw(ffi.WASMTIME_EXTERNREF.value, ext)
            return HostRef(host.n) if isinstance(host, ExternRef) else host
        finally:
            ffi.wasmtime_val_unroot(ctypes.byref(raw))

    def _invoke(
        self, func: wasmtime.Func, args: "ctypes.Array[ffi.wasmtime_val_t]", nresults: int
    ) -> "ctypes.Array[ffi.wasmtime_val_t]":
        store = self._store
        results = (ffi.wasmtime_val_t * nresults)()
        try:
            with enter_wasm(store) as trap:
                err: Any = ffi.wasmtime_func_call(
                    store._context(),
                    ctypes.byref(func._func),
                    args,
                    len(args),
                    results,
                    nresults,
                    trap,
                )
                if err:
                    raise wasmtime.WasmtimeError._from_ptr(err)
        except wasmtime.Trap as e:
            raise self._trap(e) from None
        return results

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
            if "thrown Wasm exception" in str(e):  # a start function (S0012, Q4)
                raise Trap(TrapCode.UNCAUGHT_EXCEPTION, _first_line(e)) from None
            raise LinkError(_first_line(e)) from None

    def call(self, instance: Instance, name: str, args: Sequence[Value]) -> list[Value]:
        func = cast(wasmtime.Instance, instance).exports(self._store).get(name)
        if not isinstance(func, wasmtime.Func):
            raise LookupError(f"no exported function {name}")
        store = self._store
        params, nresults = _signature(store, func)
        raw_args = (ffi.wasmtime_val_t * len(args))()
        try:
            for i, (kind, a) in enumerate(zip(params, args, strict=True)):
                if kind == "v128":
                    raw_args[i].kind = ffi.WASMTIME_V128
                    raw_args[i].of.v128 = (ctypes.c_uint8 * 16)(
                        *cast(int, a).to_bytes(16, "little")
                    )
                elif kind in ("i32", "f32"):
                    raw_args[i].kind = ffi.WASMTIME_I32 if kind == "i32" else ffi.WASMTIME_F32
                    raw_args[i].of.i32 = ctypes.c_int32(cast(int, a)).value
                elif kind in ("i64", "f64"):
                    raw_args[i].kind = ffi.WASMTIME_I64 if kind == "i64" else ffi.WASMTIME_F64
                    raw_args[i].of.i64 = ctypes.c_int64(cast(int, a)).value
                elif kind == "externref":
                    raw_args[i] = wasmtime.Val.externref(a)._new_raw(store)
                elif kind == "funcref":
                    host = cast(wasmtime.Func | None, a.handle if isinstance(a, FuncRef) else a)
                    raw_args[i] = wasmtime.Val.funcref(host)._new_raw(store)
                elif kind == "anyref" and isinstance(a, HostRef):
                    raw_args[i] = self._host_ref(a.n)
                elif a is None:  # a null anyref or exnref: the zeroed union is the null
                    raw_args[i].kind = ANYREF if kind == "anyref" else EXNREF
                else:
                    raise LookupError(f"cannot pass {a} as {kind}")
            raw_res = self._invoke(func, raw_args, nresults)
        finally:
            for i in range(len(args)):
                ffi.wasmtime_val_unroot(ctypes.byref(raw_args[i]))
        return [self._from_raw(cast(int, r.kind), r) for r in raw_res]

    def get_global(self, instance: Instance, name: str) -> Value:
        g = cast(wasmtime.Instance, instance).exports(self._store).get(name)
        if not isinstance(g, wasmtime.Global):
            raise LookupError(f"no exported global {name}")
        raw = ffi.wasmtime_val_t()
        ffi.wasmtime_global_get(self._store._context(), ctypes.byref(g._global), ctypes.byref(raw))
        return self._from_raw(cast(int, raw.kind), raw)

    @staticmethod
    def _trap(e: wasmtime.Trap) -> Exception:
        code = e.trap_code
        if code is None:
            if "thrown Wasm exception" in e.message:  # wasmtime-py 50 gives no code (S0012, Q4)
                return Trap(TrapCode.UNCAUGHT_EXCEPTION, e.message)
            return e
        return Trap(TrapCode[code.name], e.message)  # a code with no member: KeyError, as is
