# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Linking (docs/specs/S0002): resolve each import to an `Extern` and check its type.

The runtime does this, not the target: a provider is the (module, instance) of an earlier
instantiation of the store's target, or a mapping of host functions.
"""

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple, cast

from waboros.frontend.validate import functype_key, valtype_key
from waboros.targets.base import Extern, HostError, HostFunc, Raw, TargetInstance, ValidModule

_KINDS = {"func": 0, "table": 1, "mem": 2, "global": 3}
_EXTERN = {0: "func", 1: "table", 2: "memory", 3: "global"}


class LinkError(Exception):
    """Unknown import or incompatible import type."""


class Provider(NamedTuple):
    module: ValidModule
    instance: TargetInstance


def _tidx(mod: ValidModule, index: int) -> int:
    """Type index of function `index` (imports first)."""
    imported = [im for im in mod["imports"] if im[0] == "func"]
    if index < len(imported):
        return imported[index][3]
    return mod["funcs"][index - len(imported)]


def func_type(mod: ValidModule, index: int) -> tuple[list[int], list[int]]:
    """Parameter and result types of function `index`."""
    return mod["types"][_tidx(mod, index)]


def global_type(mod: ValidModule, index: int) -> tuple[int, int]:
    """(value type, mutable) of global `index`."""
    gl = [im[3] for im in mod["imports"] if im[0] == "global"] + [
        (g[0], g[1]) for g in mod["globals"]
    ]
    return gl[index]


def _key(mod: ValidModule, kind: int, index: int) -> Any:
    """Structural identity of the type of an exported function / table / global."""
    types = mod["types"]
    if kind == 0:
        return functype_key(types, _tidx(mod, index))
    if kind == 1:
        return valtype_key(types, mod["ttypes"][index])
    gt = global_type(mod, index)
    return (valtype_key(types, gt[0]), gt[1])


def host_func(f: HostFunc) -> HostFunc:
    """Any exception of a host function travels as `HostError` and comes out at the entry."""

    def wrapped(args: Sequence[Raw]) -> Sequence[Raw]:
        try:
            return f(args)
        except HostError:
            raise
        except BaseException as e:
            raise HostError(e) from None

    return wrapped


def resolve(
    mod: ValidModule, providers: Mapping[str, Provider | Mapping[str, HostFunc]]
) -> list[Extern]:
    """One `Extern` per import of `mod`, in import order."""
    out: list[Extern] = []
    ntab = 0
    for kind, modname, name, desc in cast(list[tuple[Any, ...]], mod["imports"]):
        src = providers.get(modname)
        if isinstance(src, Provider):
            exports = {n: (k, i) for n, k, i in src.module["exports"]}
            if exports.get(name, (None, 0))[0] != _KINDS[kind]:
                raise LinkError(f"unknown import {modname}.{name}")
            ex_kind, ex_idx = exports[name]
            ext = src.instance.export(name)
            if kind in ("func", "global"):
                want = (
                    functype_key(mod["types"], desc)
                    if kind == "func"
                    else (valtype_key(mod["types"], desc[0]), desc[1])
                )
                ok = _key(src.module, ex_kind, ex_idx) == want
            else:
                if kind == "table":
                    ok = _key(src.module, 1, ex_idx) == valtype_key(
                        mod["types"], mod["ttypes"][ntab]
                    )
                else:
                    ok = True
                size, mx = src.instance.limits(name)
                ok = (
                    ok
                    and size >= desc[0]
                    and (desc[1] is None or (mx is not None and mx <= desc[1]))
                )
            if not ok:
                raise LinkError(f"incompatible import type {modname}.{name}")
            out.append(ext)
        else:
            fn = None if src is None else src.get(name)
            if kind != "func" or fn is None:
                raise LinkError(f"unknown import {modname}.{name}")
            out.append(Extern("func", None, host_func(fn)))
        if kind == "table":
            ntab += 1
    return out
