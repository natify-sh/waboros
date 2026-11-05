"""call(name, *args) on a fresh instance of a wasm module, on wasmtime or a waboros target."""
from waboros.frontend import binary
from waboros.frontend.validate import validate
from waboros.runtime import link
from waboros.runtime.store import StoreState
from waboros.targets import base


def runner(target, wasm):
    if target == "wasmtime":
        import wasmtime

        eng = wasmtime.Engine()
        st = wasmtime.Store(eng)
        ex = wasmtime.Instance(st, wasmtime.Module(eng, wasm), []).exports(st)
        return lambda name, *a: ex[name](st, *a) & 0xFFFFFFFF
    mod = binary.parse(wasm)
    validate(mod)
    tg = base.TARGETS[target]()
    inst = tg.instantiate(tg.compile(mod), link.resolve(mod, {}), StoreState())
    idx = {n: i for n, k, i in mod["exports"] or () if k == 0}
    return lambda name, *a: inst.call(idx[name], list(a))[0]
